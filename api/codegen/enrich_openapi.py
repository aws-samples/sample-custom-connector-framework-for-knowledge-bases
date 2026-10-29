"""Add complete Smithy operation inputs and shared contract rules to OpenAPI."""

from __future__ import annotations

import argparse
import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

_HTTP_HEADER_TRAIT = "smithy.api#httpHeader"
_HTTP_LABEL_TRAIT = "smithy.api#httpLabel"
_HTTP_QUERY_TRAIT = "smithy.api#httpQuery"
_LOCAL_REFERENCE_PREFIX = "#/components/"
_REQUEST_MODEL_EXTENSION = "x-ccf-request-model"
_MEMBER_NAME_EXTENSION = "x-ccf-member-name"
_TAGS_TRAIT = "smithy.api#tags"
CONNECTOR_ID_PLACEHOLDER = "{connector_id}"
RUNTIME_CALLABLE_TAG = "runtime-callable"
RUNTIME_CALLABLE_EXTENSION = "x-ccf-runtime-callable"

LAMBDA_INTEGRATION_URI_PLACEHOLDER = "__CCF_API_LAMBDA_INTEGRATION_URI__"
REQUEST_VALIDATOR_NAME = "all"
_REQUEST_VALIDATOR_EXTENSION = "x-amazon-apigateway-request-validator"
_REQUEST_VALIDATORS_EXTENSION = "x-amazon-apigateway-request-validators"
_INTEGRATION_EXTENSION = "x-amazon-apigateway-integration"


@dataclass(frozen=True)
class OperationInput:
    """Smithy names and HTTP bindings for one operation input."""

    operation_name: str
    shape_name: str
    member_names: frozenset[str]
    parameter_members: dict[tuple[str, str], str]


class SmithyServiceReader:
    """Reads operation inputs and errors from one Smithy service closure."""

    def __init__(self, document: dict[str, Any]) -> None:
        self._shapes: dict[str, Any] = document["shapes"]

    def read_inputs(self) -> list[OperationInput]:
        inputs: list[OperationInput] = []
        for operation_reference in self._service()["operations"]:
            operation = self._shapes[operation_reference["target"]]
            input_shape_id = str(operation["input"]["target"])
            input_shape = self._shapes[input_shape_id]
            inputs.append(
                OperationInput(
                    operation_name=self._shape_name(str(operation_reference["target"])),
                    shape_name=self._shape_name(input_shape_id),
                    member_names=frozenset(input_shape.get("members", {})),
                    parameter_members=self._parameter_members(input_shape),
                )
            )
        return inputs

    def read_runtime_callable(self) -> frozenset[str]:
        """Return the operations a connector runtime is allowed to call."""
        return frozenset(
            self._shape_name(str(operation_reference["target"]))
            for operation_reference in self._service()["operations"]
            if RUNTIME_CALLABLE_TAG
            in self._shapes[operation_reference["target"]].get("traits", {}).get(_TAGS_TRAIT, [])
        )

    def read_errors(self) -> tuple[str, frozenset[str]]:
        error_shape_ids = {
            str(error_reference["target"])
            for operation_reference in self._service()["operations"]
            for error_reference in self._shapes[operation_reference["target"]].get("errors", [])
        }
        common_mixins = set.intersection(
            *(
                {str(mixin["target"]) for mixin in self._shapes[shape_id].get("mixins", [])}
                for shape_id in error_shape_ids
            )
        )
        if len(common_mixins) != 1:
            raise ValueError("modeled errors must share exactly one common envelope mixin")
        envelope_name = self._shape_name(common_mixins.pop())
        return envelope_name, frozenset(self._shape_name(shape_id) for shape_id in error_shape_ids)

    def _service(self) -> dict[str, Any]:
        services = [shape for shape in self._shapes.values() if shape.get("type") == "service"]
        if len(services) != 1:
            raise ValueError(f"expected exactly one Smithy service, found {len(services)}")
        return cast(dict[str, Any], services[0])

    @staticmethod
    def _parameter_members(shape: dict[str, Any]) -> dict[tuple[str, str], str]:
        bindings: dict[tuple[str, str], str] = {}
        for member_name, member in shape.get("members", {}).items():
            traits = member.get("traits", {})
            if _HTTP_LABEL_TRAIT in traits:
                bindings[("path", member_name)] = member_name
            if _HTTP_QUERY_TRAIT in traits:
                bindings[("query", str(traits[_HTTP_QUERY_TRAIT]))] = member_name
            if _HTTP_HEADER_TRAIT in traits:
                bindings[("header", str(traits[_HTTP_HEADER_TRAIT]))] = member_name
        return bindings

    @staticmethod
    def _shape_name(shape_id: str) -> str:
        return shape_id.rsplit("#", maxsplit=1)[-1]


class OpenApiEnricher:
    """Adds generated input schemas, one error envelope, and strict objects."""

    def __init__(self, smithy: dict[str, Any], openapi: dict[str, Any]) -> None:
        self._smithy_reader = SmithyServiceReader(smithy)
        self._openapi = openapi

    def enrich(self) -> None:
        operations = self._openapi_operations()
        self._add_complete_operation_inputs(operations)
        self._decorate_operations(operations)
        self._mark_runtime_callable()
        self._consolidate_error_schemas()
        self._close_object_schemas(self._openapi.get("components", {}).get("schemas", {}))

    def _add_complete_operation_inputs(self, operations: dict[str, tuple[dict[str, Any], list[Any]]]) -> None:
        schemas = self._schemas()
        for operation_input in self._smithy_reader.read_inputs():
            if operation_input.operation_name not in operations:
                raise ValueError(f"Smithy operation {operation_input.operation_name!r} is missing from OpenAPI")
            operation, inherited_parameters = operations[operation_input.operation_name]
            operation[_REQUEST_MODEL_EXTENSION] = operation_input.shape_name
            properties, required = self._body_members(operation)
            parameters = [*inherited_parameters, *operation.get("parameters", [])]
            for parameter_value in parameters:
                parameter = self._resolve(parameter_value)
                location = str(parameter["in"])
                wire_name = str(parameter["name"])
                member_name = operation_input.parameter_members.get((location, wire_name), wire_name)
                parameter[_MEMBER_NAME_EXTENSION] = member_name
                properties[member_name] = copy.deepcopy(parameter["schema"])
                if parameter.get("required") is True:
                    required.add(member_name)
            missing_members = operation_input.member_names - properties.keys()
            if missing_members:
                raise ValueError(
                    f"{operation_input.shape_name}: members missing from generated HTTP contract: "
                    f"{sorted(missing_members)}"
                )
            schemas[operation_input.shape_name] = {
                "type": "object",
                "properties": properties,
                "required": sorted(required),
                "additionalProperties": False,
            }

    def _decorate_operations(self, operations: dict[str, tuple[dict[str, Any], list[Any]]]) -> None:
        self._openapi[_REQUEST_VALIDATORS_EXTENSION] = {
            REQUEST_VALIDATOR_NAME: {
                "validateRequestBody": True,
                "validateRequestParameters": True,
            }
        }
        for operation, _ in operations.values():
            operation[_REQUEST_VALIDATOR_EXTENSION] = REQUEST_VALIDATOR_NAME
            operation[_INTEGRATION_EXTENSION] = {
                "type": "aws_proxy",
                "httpMethod": "POST",
                "uri": LAMBDA_INTEGRATION_URI_PLACEHOLDER,
            }

    def _mark_runtime_callable(self) -> None:
        """Carry the Smithy runtime-callable marker onto generated operations."""
        runtime_callable = self._smithy_reader.read_runtime_callable()
        if not runtime_callable:
            raise ValueError("no operation carries the runtime-callable tag")
        marked: set[str] = set()
        for path, path_item_value in self._openapi.get("paths", {}).items():
            path_item = cast(dict[str, Any], path_item_value)
            for operation_value in path_item.values():
                if not isinstance(operation_value, dict) or "operationId" not in operation_value:
                    continue
                operation = cast(dict[str, Any], operation_value)
                if str(operation["operationId"]) not in runtime_callable:
                    continue
                if CONNECTOR_ID_PLACEHOLDER not in path:
                    raise ValueError(f"runtime-callable {operation['operationId']} is not connector-scoped: {path}")
                operation[RUNTIME_CALLABLE_EXTENSION] = True
                marked.add(str(operation["operationId"]))
        missing = runtime_callable - marked
        if missing:
            raise ValueError(f"runtime-callable operations missing from OpenAPI: {sorted(missing)}")

    def _consolidate_error_schemas(self) -> None:
        envelope_name, error_names = self._smithy_reader.read_errors()
        schemas = self._schemas()
        component_names = {f"{error_name}ResponseContent" for error_name in error_names}
        missing_components = component_names - schemas.keys()
        if missing_components:
            raise ValueError(f"generated error schemas missing: {sorted(missing_components)}")
        component_schemas = [schemas[name] for name in sorted(component_names)]
        reference_schema = self._without_description(component_schemas[0])
        if any(self._without_description(schema) != reference_schema for schema in component_schemas[1:]):
            raise ValueError("modeled errors do not share one generated wire schema")
        schemas[envelope_name] = copy.deepcopy(component_schemas[0])
        reference_replacements = {
            f"#/components/schemas/{component_name}": f"#/components/schemas/{envelope_name}"
            for component_name in component_names
        }
        self._replace_references(self._openapi, reference_replacements)
        for component_name in component_names:
            del schemas[component_name]

    def _openapi_operations(self) -> dict[str, tuple[dict[str, Any], list[Any]]]:
        operations: dict[str, tuple[dict[str, Any], list[Any]]] = {}
        locations: dict[str, tuple[str, str]] = {}
        for path, path_item_value in self._openapi.get("paths", {}).items():
            path_item = cast(dict[str, Any], path_item_value)
            inherited_parameters = cast(list[Any], path_item.get("parameters", []))
            for method, operation_value in path_item.items():
                if not isinstance(operation_value, dict) or "operationId" not in operation_value:
                    continue
                operation = cast(dict[str, Any], operation_value)
                operation_id = str(operation["operationId"])
                if operation_id in operations:
                    previous_method, previous_path = locations[operation_id]
                    raise ValueError(
                        f"duplicate operationId {operation_id!r}: "
                        f"{previous_method.upper()} {previous_path} and {method.upper()} {path}"
                    )
                operations[operation_id] = operation, inherited_parameters
                locations[operation_id] = method, path
        return operations

    def _body_members(self, operation: dict[str, Any]) -> tuple[dict[str, Any], set[str]]:
        request_body_value = operation.get("requestBody")
        if request_body_value is None:
            return {}, set()
        request_body = self._resolve(request_body_value)
        content = cast(dict[str, Any], request_body["content"])
        media_type = cast(dict[str, Any], content["application/json"])
        body_schema = self._resolve(media_type["schema"])
        properties = copy.deepcopy(cast(dict[str, Any], body_schema.get("properties", {})))
        required = set(cast(list[str], body_schema.get("required", [])))
        return properties, required

    def _resolve(self, value: Any) -> dict[str, Any]:
        schema = cast(dict[str, Any], value)
        reference = schema.get("$ref")
        if reference is None:
            return schema
        if not isinstance(reference, str) or not reference.startswith(_LOCAL_REFERENCE_PREFIX):
            raise ValueError(f"unsupported reference: {reference}")
        resolved: Any = self._openapi
        for segment in reference.removeprefix("#/").split("/"):
            resolved = resolved[segment]
        return cast(dict[str, Any], resolved)

    def _schemas(self) -> dict[str, Any]:
        return cast(dict[str, Any], self._openapi["components"]["schemas"])

    @classmethod
    def _close_object_schemas(cls, value: Any) -> None:
        if isinstance(value, dict):
            if "properties" in value or (value.get("type") == "object" and "additionalProperties" not in value):
                value["additionalProperties"] = False
            for nested_value in value.values():
                cls._close_object_schemas(nested_value)
        elif isinstance(value, list):
            for nested_value in value:
                cls._close_object_schemas(nested_value)

    @classmethod
    def _replace_references(cls, value: Any, replacements: dict[str, str]) -> None:
        if isinstance(value, dict):
            reference = value.get("$ref")
            if isinstance(reference, str) and reference in replacements:
                value["$ref"] = replacements[reference]
            for nested_value in value.values():
                cls._replace_references(nested_value, replacements)
        elif isinstance(value, list):
            for nested_value in value:
                cls._replace_references(nested_value, replacements)

    @staticmethod
    def _without_description(schema: dict[str, Any]) -> dict[str, Any]:
        comparable = copy.deepcopy(schema)
        comparable.pop("description", None)
        return comparable


def _load_json(path: Path) -> dict[str, Any]:
    document: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"expected an object in {path}")
    return cast(dict[str, Any], document)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--openapi", type=Path, required=True)
    arguments = parser.parse_args()

    smithy = _load_json(arguments.model)
    openapi = _load_json(arguments.openapi)
    OpenApiEnricher(smithy, openapi).enrich()
    arguments.openapi.write_text(f"{json.dumps(openapi, indent=4)}\n", encoding="utf-8")


if __name__ == "__main__":
    main()
