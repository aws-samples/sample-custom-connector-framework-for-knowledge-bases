"""Add to the Smithy-emitted OpenAPI document the complete inputs, the CCF operation extensions, a named
schema per patterned string, one error envelope, closed objects, and API Gateway's validation."""

from __future__ import annotations

import argparse
import copy
import json
from collections.abc import Iterator, Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

from ccf_api_models.base import ValueModel

CALLERS_TRAIT = "ccf#callers"
CLEARABLE_TRAIT = "ccf#clearable"
"""An update member that null clears and omission keeps."""
_DOCUMENTATION_TRAIT = "smithy.api#documentation"
_DEFAULT_TRAIT = "smithy.api#default"
_HTTP_HEADER_TRAIT = "smithy.api#httpHeader"
_HTTP_LABEL_TRAIT = "smithy.api#httpLabel"
_HTTP_QUERY_TRAIT = "smithy.api#httpQuery"
_IDEMPOTENCY_TOKEN_TRAIT = "smithy.api#idempotencyToken"  # noqa: S105 - a trait ID, not a secret
_LENGTH_TRAIT = "smithy.api#length"
_PAGINATED_TRAIT = "smithy.api#paginated"
_PATTERN_TRAIT = "smithy.api#pattern"
_RETRY_SAFE_TRAITS = frozenset({"smithy.api#readonly", "smithy.api#idempotent"})
_NAMED_STRING_TRAITS = frozenset({_DOCUMENTATION_TRAIT, _LENGTH_TRAIT, _PATTERN_TRAIT})
_ERROR_CODE_SHAPE = "ccf#ErrorCode"
_NAMESPACE = "ccf#"
_SCHEMAS = "#/components/schemas/"

DESTINATION_ID_PLACEHOLDER = "{destination_id}"
RUNTIME_CALLER = "RUNTIME"
REQUEST_VALIDATOR_NAME = "all"
BAD_REQUEST_CODE = "BAD_REQUEST"
BAD_REQUEST_MESSAGE = "The request is invalid"
"""The message of every 400, from API Gateway's validator or the backend; it never quotes the request."""
GATEWAY_REJECTIONS = ("BAD_REQUEST_BODY", "BAD_REQUEST_PARAMETERS")
CORRELATION_HEADER = "x-correlation-id"


class Extension(StrEnum):
    REQUEST_MODEL = "x-ccf-request-model"
    MEMBER_NAME = "x-ccf-member-name"
    CALLERS = "x-ccf-callers"
    CLIENT_TOKEN_MEMBER = "x-ccf-client-token-member"  # noqa: S105 - an extension name, not a secret
    PAGINATED = "x-ccf-paginated"
    RETRY_SAFE = "x-ccf-retry-safe"
    REQUEST_VALIDATOR = "x-amazon-apigateway-request-validator"
    REQUEST_VALIDATORS = "x-amazon-apigateway-request-validators"
    GATEWAY_RESPONSES = "x-amazon-apigateway-gateway-responses"


class SmithyOperation(ValueModel):
    """What the enricher writes for one operation, read from its Smithy shape and its input."""

    name: str
    input_shape_name: str
    member_names: frozenset[str]
    parameter_members: Mapping[tuple[str, str], str]
    """The input member bound to each `(location, wire name)` parameter."""
    clearable_members: frozenset[str]
    client_token_member: str | None
    callers: tuple[str, ...]
    retry_safe: bool
    pagination: Mapping[str, str] | None


class ModeledErrors(ValueModel):
    envelope_name: str
    """The mixin every modeled error shares, which becomes the one error schema."""
    error_names: frozenset[str]


class PatternedReference(ValueModel):
    """A structure member that targets a patterned string, directly or as its list's items."""

    structure_name: str
    member_name: str
    schema_name: str
    list_items: bool
    description: str | None
    default: str | None
    """The member's Smithy default, which the named schema can't carry because other members share it."""


class LocatedOperation(ValueModel):
    smithy: SmithyOperation
    path: str
    openapi: Any
    """The OpenAPI operation object, patched in place."""


class SmithyServiceReader:
    """Reads the operations, errors, and patterned strings of a Smithy AST."""

    def __init__(self, document: dict[str, Any]) -> None:
        self._shapes: dict[str, Any] = document["shapes"]

    def read_operations(self) -> list[SmithyOperation]:
        return [self._operation(shape_id) for shape_id in self._shape_ids_of("operation")]

    def read_errors(self) -> ModeledErrors:
        [service_id] = self._shape_ids_of("service")
        error_ids = {str(reference["target"]) for reference in self._shapes[service_id].get("errors", [])}
        error_ids.update(
            str(reference["target"])
            for operation_id in self._shape_ids_of("operation")
            for reference in self._shapes[operation_id].get("errors", [])
        )
        common_mixins = set.intersection(
            *({str(mixin["target"]) for mixin in self._shapes[shape_id].get("mixins", [])} for shape_id in error_ids)
        )
        if len(common_mixins) != 1:
            raise ValueError("modeled errors must share exactly one common envelope mixin")
        return ModeledErrors(
            envelope_name=self._shape_name(common_mixins.pop()),
            error_names=frozenset(self._shape_name(shape_id) for shape_id in error_ids),
        )

    def read_error_codes(self) -> frozenset[str]:
        return frozenset(self._shapes[_ERROR_CODE_SHAPE]["members"])

    def read_patterned_references(self) -> list[PatternedReference]:
        references: list[PatternedReference] = []
        for structure_id in self._shape_ids_of("structure"):
            for member_name, member in self._members(self._shapes[structure_id]).items():
                target = self._shapes.get(str(member["target"]), {})
                list_items = target.get("type") == "list"
                named_id = str(target["member"]["target"] if list_items else member["target"])
                named = self._shapes.get(named_id, {})
                if named.get("type") != "string" or _PATTERN_TRAIT not in named.get("traits", {}):
                    continue
                traits = member.get("traits", {})
                references.append(
                    PatternedReference(
                        structure_name=self._shape_name(structure_id),
                        member_name=member_name,
                        schema_name=self._shape_name(named_id),
                        list_items=list_items,
                        description=traits.get(_DOCUMENTATION_TRAIT),
                        default=traits.get(_DEFAULT_TRAIT),
                    )
                )
        return references

    def read_named_string_schema(self, schema_name: str) -> dict[str, Any]:
        """Return a patterned string's JSON schema; a trait the schema can't carry fails."""
        traits = cast(dict[str, Any], self._shapes[f"{_NAMESPACE}{schema_name}"]["traits"])
        unsupported = traits.keys() - _NAMED_STRING_TRAITS
        if unsupported:
            raise ValueError(f"{schema_name}: a named string schema can't carry {sorted(unsupported)}")
        length = cast(dict[str, int], traits.get(_LENGTH_TRAIT, {}))
        schema = {
            "type": "string",
            "pattern": traits[_PATTERN_TRAIT],
            "minLength": length.get("min"),
            "maxLength": length.get("max"),
            "description": traits.get(_DOCUMENTATION_TRAIT),
        }
        return {key: value for key, value in schema.items() if value is not None}

    def _operation(self, operation_id: str) -> SmithyOperation:
        traits = cast(dict[str, Any], self._shapes[operation_id].get("traits", {}))
        if CALLERS_TRAIT not in traits:
            raise ValueError(f"{self._shape_name(operation_id)} has no @callers trait")
        input_shape_id = str(self._shapes[operation_id]["input"]["target"])
        members = self._members(self._shapes[input_shape_id])
        member_traits = {name: cast(dict[str, Any], member.get("traits", {})) for name, member in members.items()}
        client_token_members = [name for name, traits in member_traits.items() if _IDEMPOTENCY_TOKEN_TRAIT in traits]
        if len(client_token_members) > 1:
            raise ValueError(f"{self._shape_name(input_shape_id)} has more than one @idempotencyToken member")
        return SmithyOperation(
            name=self._shape_name(operation_id),
            input_shape_name=self._shape_name(input_shape_id),
            member_names=frozenset(members),
            parameter_members=self._parameter_members(member_traits),
            clearable_members=frozenset(name for name, traits in member_traits.items() if CLEARABLE_TRAIT in traits),
            client_token_member=client_token_members[0] if client_token_members else None,
            callers=tuple(traits[CALLERS_TRAIT]),
            retry_safe=bool(_RETRY_SAFE_TRAITS & traits.keys()),
            pagination=traits.get(_PAGINATED_TRAIT),
        )

    def _members(self, shape: dict[str, Any]) -> dict[str, Any]:
        """Return the shape's members after the members of its mixins, as the OpenAPI plugin orders them."""
        members: dict[str, Any] = {}
        for mixin in shape.get("mixins", []):
            members.update(self._members(self._shapes[mixin["target"]]))
        members.update(shape.get("members", {}))
        return members

    def _shape_ids_of(self, shape_type: str) -> list[str]:
        return [
            shape_id
            for shape_id, shape in self._shapes.items()
            if shape_id.startswith(_NAMESPACE) and shape["type"] == shape_type
        ]

    @staticmethod
    def _parameter_members(member_traits: dict[str, dict[str, Any]]) -> dict[tuple[str, str], str]:
        bindings: dict[tuple[str, str], str] = {}
        for member_name, traits in member_traits.items():
            if _HTTP_LABEL_TRAIT in traits:
                bindings[("path", member_name)] = member_name
            if _HTTP_QUERY_TRAIT in traits:
                bindings[("query", str(traits[_HTTP_QUERY_TRAIT]))] = member_name
            if _HTTP_HEADER_TRAIT in traits:
                bindings[("header", str(traits[_HTTP_HEADER_TRAIT]))] = member_name
        return bindings

    @staticmethod
    def _shape_name(shape_id: str) -> str:
        return shape_id.removeprefix(_NAMESPACE)


class OpenApiEnricher:
    """Patches one OpenAPI document in place from the Smithy AST it was generated from."""

    def __init__(self, smithy: dict[str, Any], openapi: dict[str, Any]) -> None:
        self._smithy_reader = SmithyServiceReader(smithy)
        self._openapi = openapi

    def enrich(self) -> None:
        for located in self._located_operations():
            self._allow_null_for_clearable_members(located)
            self._add_complete_input(located)
            self._mark_operation(located)
        self._name_patterned_strings()
        self._consolidate_error_schemas()
        for json_object in _json_objects(self._schemas()):
            if "properties" in json_object or json_object.get("type") == "object":
                json_object.setdefault("additionalProperties", False)
        self._add_gateway_validation()

    def _located_operations(self) -> list[LocatedOperation]:
        paths = {
            str(operation["operationId"]): (path, operation)
            for path, path_item in self._openapi["paths"].items()
            for operation in path_item.values()
        }
        return [
            LocatedOperation(smithy=operation, path=paths[operation.name][0], openapi=paths[operation.name][1])
            for operation in self._smithy_reader.read_operations()
        ]

    def _allow_null_for_clearable_members(self, located: LocatedOperation) -> None:
        """Let each `@clearable` body member be null, in the body schema API Gateway validates.

        API Gateway's draft 4 models don't support `nullable`, so the member becomes `anyOf` its schema
        and `{"type": "null"}`. The complete input schema copies the body schema, so it allows null too.
        """
        properties = self._body_schema(located.openapi).get("properties", {})
        for member_name in sorted(located.smithy.clearable_members):
            if member_name not in properties:
                raise ValueError(
                    f"{located.smithy.input_shape_name}.{member_name}: @clearable is only for body members"
                )
            properties[member_name] = {"anyOf": [properties[member_name], {"type": "null"}]}

    def _add_complete_input(self, located: LocatedOperation) -> None:
        """Write the input shape as one schema of its body and parameter members, and name each parameter's member."""
        body_schema = self._body_schema(located.openapi)
        properties = copy.deepcopy(cast(dict[str, Any], body_schema.get("properties", {})))
        required = set(cast(list[str], body_schema.get("required", [])))
        for parameter in located.openapi.get("parameters", []):
            wire_name = str(parameter["name"])
            member_name = located.smithy.parameter_members.get((str(parameter["in"]), wire_name), wire_name)
            parameter[Extension.MEMBER_NAME] = member_name
            properties[member_name] = copy.deepcopy(parameter["schema"])
            if parameter.get("required") is True:
                required.add(member_name)
        missing_members = located.smithy.member_names - properties.keys()
        if missing_members:
            raise ValueError(
                f"{located.smithy.input_shape_name}: members missing from OpenAPI: {sorted(missing_members)}"
            )
        located.openapi[Extension.REQUEST_MODEL] = located.smithy.input_shape_name
        self._schemas()[located.smithy.input_shape_name] = {
            "type": "object",
            "properties": properties,
            "required": sorted(required),
        }

    def _mark_operation(self, located: LocatedOperation) -> None:
        smithy_operation, operation = located.smithy, located.openapi
        if RUNTIME_CALLER in smithy_operation.callers and DESTINATION_ID_PLACEHOLDER not in located.path:
            raise ValueError(f"{smithy_operation.name} admits {RUNTIME_CALLER} but isn't under a destination")
        operation[Extension.CALLERS] = list(smithy_operation.callers)
        operation[Extension.RETRY_SAFE] = smithy_operation.retry_safe
        operation[Extension.REQUEST_VALIDATOR] = REQUEST_VALIDATOR_NAME
        if smithy_operation.client_token_member is not None:
            operation[Extension.CLIENT_TOKEN_MEMBER] = smithy_operation.client_token_member
        if smithy_operation.pagination is not None:
            operation[Extension.PAGINATED] = dict(smithy_operation.pagination)

    def _name_patterned_strings(self) -> None:
        """Point every component member that targets a patterned string at one schema named after the string.

        datamodel-codegen then writes one type per ID and name; parameter schemas stay inline.
        """
        schemas = self._schemas()
        named: set[str] = set()
        for reference in self._smithy_reader.read_patterned_references():
            for component_name in (reference.structure_name, f"{reference.structure_name}Content"):
                properties = cast(dict[str, Any], schemas.get(component_name, {}).get("properties", {}))
                if reference.member_name not in properties:
                    continue
                member_schema = cast(dict[str, Any], properties[reference.member_name])
                target = cast(dict[str, Any], member_schema["items"]) if reference.list_items else member_schema
                target.clear()
                target["$ref"] = f"{_SCHEMAS}{reference.schema_name}"
                if reference.description is not None and not reference.list_items:
                    target["description"] = reference.description
                if reference.default is not None and not reference.list_items:
                    target["default"] = reference.default
                named.add(reference.schema_name)
        colliding = named & schemas.keys()
        if colliding:
            raise ValueError(f"patterned strings collide with generated schemas: {sorted(colliding)}")
        schemas.update({name: self._smithy_reader.read_named_string_schema(name) for name in sorted(named)})

    def _consolidate_error_schemas(self) -> None:
        errors = self._smithy_reader.read_errors()
        schemas = self._schemas()
        component_names = sorted(f"{error_name}ResponseContent" for error_name in errors.error_names)
        comparable = [
            {key: value for key, value in schemas[name].items() if key != "description"} for name in component_names
        ]
        if any(schema != comparable[0] for schema in comparable[1:]):
            raise ValueError("modeled errors do not share one generated wire schema")
        schemas[errors.envelope_name] = copy.deepcopy(schemas[component_names[0]])
        replaced = {f"{_SCHEMAS}{name}" for name in component_names}
        for json_object in _json_objects(self._openapi):
            if json_object.get("$ref") in replaced:
                json_object["$ref"] = f"{_SCHEMAS}{errors.envelope_name}"
        for component_name in component_names:
            del schemas[component_name]

    def _add_gateway_validation(self) -> None:
        """Validate every request's body and parameters, and answer a rejection with the error envelope."""
        if BAD_REQUEST_CODE not in self._smithy_reader.read_error_codes():
            raise ValueError(f"{BAD_REQUEST_CODE} is not a member of {_ERROR_CODE_SHAPE}")
        self._openapi[Extension.REQUEST_VALIDATORS] = {
            REQUEST_VALIDATOR_NAME: {"validateRequestBody": True, "validateRequestParameters": True}
        }
        envelope = {"code": BAD_REQUEST_CODE, "message": BAD_REQUEST_MESSAGE, "request_id": "$context.requestId"}
        rejection = {
            "statusCode": 400,
            "responseTemplates": {"application/json": json.dumps(envelope, separators=(",", ":"))},
            "responseParameters": {
                f"gatewayresponse.header.{CORRELATION_HEADER}": f"method.request.header.{CORRELATION_HEADER}"
            },
        }
        self._openapi[Extension.GATEWAY_RESPONSES] = {name: copy.deepcopy(rejection) for name in GATEWAY_REJECTIONS}

    def _body_schema(self, operation: dict[str, Any]) -> dict[str, Any]:
        """The operation's JSON request body schema, or an empty schema when it has no body."""
        if "requestBody" not in operation:
            return {}
        reference = str(operation["requestBody"]["content"]["application/json"]["schema"]["$ref"])
        return cast(dict[str, Any], self._schemas()[reference.removeprefix(_SCHEMAS)])

    def _schemas(self) -> dict[str, Any]:
        return cast(dict[str, Any], self._openapi["components"]["schemas"])


def _json_objects(json_node: Any) -> Iterator[dict[str, Any]]:
    """Yield every object in a JSON document, each before the objects nested in it."""
    if isinstance(json_node, dict):
        yield json_node
        for nested_node in json_node.values():
            yield from _json_objects(nested_node)
    elif isinstance(json_node, list):
        for nested_node in json_node:
            yield from _json_objects(nested_node)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--openapi", type=Path, required=True)
    arguments = parser.parse_args()

    smithy = json.loads(arguments.model.read_text(encoding="utf-8"))
    openapi = json.loads(arguments.openapi.read_text(encoding="utf-8"))
    OpenApiEnricher(smithy, openapi).enrich()
    arguments.openapi.write_text(f"{json.dumps(openapi, indent=4)}\n", encoding="utf-8")


if __name__ == "__main__":
    main()
