"""Compare generated OpenAPI with operation metadata derived from the Smithy AST."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from enrich_openapi import (
    CONNECTOR_ID_PLACEHOLDER,
    LAMBDA_INTEGRATION_URI_PLACEHOLDER,
    REQUEST_VALIDATOR_NAME,
    RUNTIME_CALLABLE_EXTENSION,
    RUNTIME_CALLABLE_TAG,
    SmithyServiceReader,
)

_SMITHY_HTTP_TRAIT = "smithy.api#http"
_SMITHY_HTTP_ERROR_TRAIT = "smithy.api#httpError"
_SMITHY_REQUIRED_TRAIT = "smithy.api#required"
_SMITHY_ERROR_TRAIT = "smithy.api#error"
_HTTP_METHODS = frozenset({"delete", "get", "head", "options", "patch", "post", "put", "trace"})
_ERROR_MEMBER_NAMES = frozenset({"code", "message", "request_id"})
_REQUEST_VALIDATOR_EXTENSION = "x-amazon-apigateway-request-validator"
_REQUEST_VALIDATORS_EXTENSION = "x-amazon-apigateway-request-validators"
_INTEGRATION_EXTENSION = "x-amazon-apigateway-integration"
_SIGV4_AUTHTYPE = "awsSigv4"


@dataclass(frozen=True)
class HttpOperation:
    """One operation's generated HTTP contract."""

    name: str
    method: str
    path: str
    success_status: str
    error_statuses: frozenset[str]


@dataclass(frozen=True)
class ContractDocuments:
    """Inputs needed for one contract comparison."""

    smithy: dict[str, Any]
    openapi: dict[str, Any]


class SmithyContractReader:
    """Reads operation contracts from a Smithy AST."""

    def __init__(self, document: dict[str, Any]) -> None:
        self._shapes: dict[str, Any] = document["shapes"]

    def read(self) -> dict[str, HttpOperation]:
        service = self._find_service()
        return {
            self._shape_name(reference["target"]): self._read_operation(reference["target"])
            for reference in service["operations"]
        }

    def _find_service(self) -> dict[str, Any]:
        services = [shape for shape in self._shapes.values() if shape.get("type") == "service"]
        if len(services) != 1:
            raise ValueError(f"expected exactly one Smithy service, found {len(services)}")
        return cast(dict[str, Any], services[0])

    def _read_operation(self, shape_id: str) -> HttpOperation:
        operation = self._shapes[shape_id]
        http = operation["traits"][_SMITHY_HTTP_TRAIT]
        return HttpOperation(
            name=self._shape_name(shape_id),
            method=str(http["method"]).lower(),
            path=str(http["uri"]),
            success_status=str(http["code"]),
            error_statuses=frozenset(
                self._error_status(reference["target"]) for reference in operation.get("errors", [])
            ),
        )

    def _error_status(self, shape_id: str) -> str:
        return str(self._shapes[shape_id]["traits"][_SMITHY_HTTP_ERROR_TRAIT])

    @staticmethod
    def _shape_name(shape_id: str) -> str:
        return shape_id.rsplit("#", maxsplit=1)[-1]


def _read_openapi_operations(
    document: dict[str, Any],
) -> list[tuple[str, str, str, dict[str, Any]]]:
    operations: list[tuple[str, str, str, dict[str, Any]]] = []
    locations: dict[str, tuple[str, str]] = {}
    for path, path_item in document.get("paths", {}).items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method not in _HTTP_METHODS:
                continue
            if not isinstance(operation, dict):
                raise ValueError(f"{method.upper()} {path}: operation must be an object")
            operation_id = operation.get("operationId")
            if not isinstance(operation_id, str):
                raise ValueError(f"{method.upper()} {path}: operationId must be a string")
            if operation_id in locations:
                previous_method, previous_path = locations[operation_id]
                raise ValueError(
                    f"duplicate operationId {operation_id!r}: "
                    f"{previous_method.upper()} {previous_path} and {method.upper()} {path}"
                )
            locations[operation_id] = method, path
            operations.append((operation_id, method, path, operation))
    return operations


class OpenApiContractReader:
    """Reads operation contracts from an OpenAPI document."""

    def __init__(self, document: dict[str, Any]) -> None:
        self._document = document

    def read(self) -> dict[str, HttpOperation]:
        operations: dict[str, HttpOperation] = {}
        for operation_id, method, path, operation in _read_openapi_operations(self._document):
            responses = operation.get("responses", {})
            operations[operation_id] = HttpOperation(
                name=operation_id,
                method=method,
                path=path,
                success_status=self._success_status(responses),
                error_statuses=frozenset(status for status in responses if status.startswith(("4", "5"))),
            )
        return operations

    @staticmethod
    def _success_status(responses: dict[str, Any]) -> str:
        statuses = [status for status in responses if status.startswith("2")]
        return statuses[0] if len(statuses) == 1 else ""


class ContractAsserter:
    """Checks OpenAPI operations and schemas against the Smithy AST."""

    def __init__(self, documents: ContractDocuments) -> None:
        self._smithy = documents.smithy
        self._openapi = documents.openapi

    def assert_valid(self) -> list[str]:
        expected = SmithyContractReader(self._smithy).read()
        actual = OpenApiContractReader(self._openapi).read()
        failures = self._compare_operations(expected, actual)
        failures.extend(self._check_api_gateway_contract(expected))
        failures.extend(self._check_sigv4_security())
        failures.extend(self._check_operation_schemas())
        failures.extend(self._check_required_members())
        failures.extend(self._check_error_members())
        failures.extend(self._check_closed_object_schemas())
        failures.extend(self._check_runtime_callable_parity())
        return failures

    def _check_runtime_callable_parity(self) -> list[str]:
        """The Smithy tag is the only source of runtime-callable operations."""
        expected = SmithyServiceReader(self._smithy).read_runtime_callable()
        actual = {
            operation_id
            for operation_id, _, _, operation in _read_openapi_operations(self._openapi)
            if operation.get(RUNTIME_CALLABLE_EXTENSION) is True
        }
        if not expected:
            return [f"no operation carries the {RUNTIME_CALLABLE_TAG!r} tag"]
        failures = [
            f"{name}: tagged {RUNTIME_CALLABLE_TAG!r} but not marked in OpenAPI" for name in sorted(expected - actual)
        ]
        failures.extend(
            f"{name}: marked runtime-callable in OpenAPI without the Smithy tag" for name in sorted(actual - expected)
        )
        failures.extend(
            f"{name}: runtime-callable operation is not connector-scoped: {path}"
            for name, _, path, operation in _read_openapi_operations(self._openapi)
            if operation.get(RUNTIME_CALLABLE_EXTENSION) is True and CONNECTOR_ID_PLACEHOLDER not in path
        )
        return failures

    @staticmethod
    def _compare_operations(expected: dict[str, HttpOperation], actual: dict[str, HttpOperation]) -> list[str]:
        failures = [f"missing operation: {name}" for name in sorted(expected.keys() - actual.keys())]
        failures.extend(f"unexpected operation: {name}" for name in sorted(actual.keys() - expected.keys()))
        for name in sorted(expected.keys() & actual.keys()):
            if expected[name] != actual[name]:
                failures.append(f"{name}: Smithy {expected[name]} != OpenAPI {actual[name]}")
        return failures

    def _check_api_gateway_contract(self, expected: dict[str, HttpOperation]) -> list[str]:
        failures: list[str] = []
        validators = self._openapi.get(_REQUEST_VALIDATORS_EXTENSION)
        expected_validators = {
            REQUEST_VALIDATOR_NAME: {
                "validateRequestBody": True,
                "validateRequestParameters": True,
            }
        }
        if validators != expected_validators:
            failures.append(f"request validators {validators!r} != {expected_validators!r}")

        operation_documents = self._operation_documents()
        integration_uris: set[str] = set()
        expected_integration = {
            "type": "aws_proxy",
            "httpMethod": "POST",
            "uri": LAMBDA_INTEGRATION_URI_PLACEHOLDER,
        }
        for operation_name in sorted(expected):
            operation = operation_documents.get(operation_name)
            if operation is None:
                continue
            integration = operation.get(_INTEGRATION_EXTENSION)
            if integration != expected_integration:
                failures.append(f"{operation_name}: integration {integration!r} != {expected_integration!r}")
            if isinstance(integration, dict):
                integration_uri = integration.get("uri")
                if isinstance(integration_uri, str):
                    integration_uris.add(integration_uri)
            validator_reference = operation.get(_REQUEST_VALIDATOR_EXTENSION)
            if validator_reference != REQUEST_VALIDATOR_NAME:
                failures.append(
                    f"{operation_name}: request validator {validator_reference!r} != {REQUEST_VALIDATOR_NAME!r}"
                )
        if integration_uris != {LAMBDA_INTEGRATION_URI_PLACEHOLDER}:
            failures.append(f"operation integrations must use one unique Lambda URI placeholder: {integration_uris!r}")
        return failures

    def _check_sigv4_security(self) -> list[str]:
        failures: list[str] = []
        schemes = self._openapi.get("components", {}).get("securitySchemes", {})
        if not isinstance(schemes, dict):
            return ["components.securitySchemes must be an object"]
        sigv4_schemes = {
            name: scheme
            for name, scheme in schemes.items()
            if isinstance(scheme, dict) and scheme.get("x-amazon-apigateway-authtype") == _SIGV4_AUTHTYPE
        }
        if len(sigv4_schemes) != 1:
            return [f"expected exactly one generated SigV4 security scheme, found {len(sigv4_schemes)}"]
        scheme_name, scheme = next(iter(sigv4_schemes.items()))
        if set(schemes) != {scheme_name}:
            failures.append(f"unexpected additional security schemes: {sorted(set(schemes) - {scheme_name})}")
        expected_properties = {
            "type": "apiKey",
            "name": "Authorization",
            "in": "header",
            "x-amazon-apigateway-authtype": _SIGV4_AUTHTYPE,
        }
        for property_name, expected_value in expected_properties.items():
            if scheme.get(property_name) != expected_value:
                failures.append(f"{scheme_name}: {property_name} {scheme.get(property_name)!r} != {expected_value!r}")
        expected_global_security: list[dict[str, list[str]]] = [{scheme_name: []}]
        if self._openapi.get("security") != expected_global_security:
            failures.append(f"global security {self._openapi.get('security')!r} != {expected_global_security!r}")
        for operation_name, operation in sorted(self._operation_documents().items()):
            if "security" in operation and operation["security"] != expected_global_security:
                failures.append(
                    f"{operation_name}: operation security {operation['security']!r} != {expected_global_security!r}"
                )
        return failures

    def _operation_documents(self) -> dict[str, dict[str, Any]]:
        return {operation_id: operation for operation_id, _, _, operation in _read_openapi_operations(self._openapi)}

    def _check_operation_schemas(self) -> list[str]:
        failures: list[str] = []
        schemas = self._schemas()
        for operation_reference in self._service()["operations"]:
            operation = self._smithy["shapes"][operation_reference["target"]]
            operation_name = operation_reference["target"].rsplit("#", maxsplit=1)[-1]
            input_name = operation["input"]["target"].rsplit("#", maxsplit=1)[-1]
            if input_name not in schemas:
                failures.append(f"{operation_name}: complete input schema not found: {input_name}")
            output_target = operation["output"]["target"]
            if output_target == "smithy.api#Unit":
                continue
            output_name = output_target.rsplit("#", maxsplit=1)[-1]
            if self._find_schema_name(output_name) is None:
                failures.append(f"{operation_name}: output schema not found: {output_name}")
        return failures

    def _check_required_members(self) -> list[str]:
        failures: list[str] = []
        for shape_id, shape in self._smithy["shapes"].items():
            if shape.get("type") != "structure":
                continue
            required = self._required_members(shape)
            schema_name = self._find_schema_name(shape_id.rsplit("#", maxsplit=1)[-1])
            if schema_name is None:
                continue
            generated_required = set(self._schemas()[schema_name].get("required", []))
            missing = required - generated_required
            if missing:
                failures.append(f"{schema_name}: required members missing from OpenAPI: {sorted(missing)}")
        return failures

    def _check_error_members(self) -> list[str]:
        failures: list[str] = []
        error_shapes = [
            (shape_id, shape)
            for shape_id, shape in self._smithy["shapes"].items()
            if _SMITHY_ERROR_TRAIT in shape.get("traits", {})
        ]
        common_mixins = set.intersection(
            *({mixin["target"] for mixin in shape.get("mixins", [])} for _, shape in error_shapes)
        )
        if len(common_mixins) != 1:
            return ["modeled errors do not share exactly one envelope mixin"]
        envelope_name = common_mixins.pop().rsplit("#", maxsplit=1)[-1]
        envelope_schema = self._schemas().get(envelope_name)
        if envelope_schema is None:
            return [f"{envelope_name}: common generated error schema not found"]
        properties = set(envelope_schema.get("properties", {}))
        generated_required = set(envelope_schema.get("required", []))
        if not _ERROR_MEMBER_NAMES.issubset(properties):
            failures.append(f"{envelope_name}: common error properties missing")
        if not _ERROR_MEMBER_NAMES.issubset(generated_required):
            failures.append(f"{envelope_name}: common error properties are not all required")
        for shape_id, shape in error_shapes:
            shape_name = shape_id.rsplit("#", maxsplit=1)[-1]
            if not _ERROR_MEMBER_NAMES.issubset(self._required_members(shape)):
                failures.append(f"{shape_name}: error members are not all required")
            duplicate_name = f"{shape_name}ResponseContent"
            if duplicate_name in self._schemas():
                failures.append(f"{duplicate_name}: duplicate status-specific error schema remains")
        return failures

    def _check_closed_object_schemas(self) -> list[str]:
        return [
            f"{name}: object schema permits additional properties"
            for name, schema in sorted(self._schemas().items())
            if (schema.get("type") == "object" or "properties" in schema)
            and schema.get("additionalProperties") is not False
        ]

    def _service(self) -> dict[str, Any]:
        services = [shape for shape in self._smithy["shapes"].values() if shape.get("type") == "service"]
        if len(services) != 1:
            raise ValueError(f"expected exactly one Smithy service, found {len(services)}")
        return cast(dict[str, Any], services[0])

    def _find_schema_name(self, shape_name: str) -> str | None:
        schemas = self._schemas()
        candidates = [shape_name]
        for suffix in ("Request", "Response"):
            if shape_name.endswith(suffix):
                candidates.append(f"{shape_name.removesuffix(suffix)}{suffix}Content")
        for candidate in candidates:
            if candidate in schemas:
                return candidate
        return None

    def _schemas(self) -> dict[str, Any]:
        schemas: object = self._openapi.get("components", {}).get("schemas", {})
        return cast(dict[str, Any], schemas)

    def _required_members(self, shape: dict[str, Any]) -> set[str]:
        members = dict(shape.get("members", {}))
        for mixin in shape.get("mixins", []):
            mixin_shape = self._smithy["shapes"][mixin["target"]]
            members.update(self._members_with_mixins(mixin_shape))
        return {name for name, member in members.items() if _SMITHY_REQUIRED_TRAIT in member.get("traits", {})}

    def _members_with_mixins(self, shape: dict[str, Any]) -> dict[str, Any]:
        members = dict(shape.get("members", {}))
        for mixin in shape.get("mixins", []):
            mixin_shape = self._smithy["shapes"][mixin["target"]]
            members.update(self._members_with_mixins(mixin_shape))
        return members


def _load_json(path: str) -> dict[str, Any]:
    document: object = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"expected an object in {path}")
    return document


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare generated OpenAPI with the Smithy AST.")
    parser.add_argument("--model", default="generated/model.json")
    parser.add_argument("--openapi", default="generated/openapi.json")
    args = parser.parse_args()

    documents = ContractDocuments(smithy=_load_json(args.model), openapi=_load_json(args.openapi))
    failures = ContractAsserter(documents).assert_valid()
    if failures:
        print("CONTRACT DRIFT:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        sys.exit(1)
    operation_count = len(SmithyContractReader(documents.smithy).read())
    runtime_count = len(SmithyServiceReader(documents.smithy).read_runtime_callable())
    print(f"contract OK: {operation_count} Smithy-derived operations verified, {runtime_count} runtime-callable")


if __name__ == "__main__":
    main()
