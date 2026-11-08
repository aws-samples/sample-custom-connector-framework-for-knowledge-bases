"""The enricher's output, on a two-operation Smithy AST and the OpenAPI document the plugin emits for it."""

import json
from typing import Any

import pytest
from enrich_openapi import (
    BAD_REQUEST_MESSAGE,
    CALLERS_TRAIT,
    CLEARABLE_TRAIT,
    GATEWAY_REJECTIONS,
    REQUEST_VALIDATOR_NAME,
    Extension,
    OpenApiEnricher,
)

PATH = "/destinations/{destination_id}/things"
SCHEMAS = "#/components/schemas/"


def _smithy() -> dict[str, Any]:
    label = {"target": "ccf#DestinationId", "traits": {"smithy.api#httpLabel": {}, "smithy.api#required": {}}}
    return {
        "shapes": {
            "ccf#Things": {"type": "service", "errors": [{"target": "ccf#BadRequestException"}]},
            "ccf#ErrorCode": {"type": "enum", "members": {"BAD_REQUEST": {"target": "smithy.api#Unit"}}},
            "ccf#ErrorEnvelope": {"type": "structure", "members": {}, "traits": {"smithy.api#mixin": {}}},
            "ccf#BadRequestException": {"type": "structure", "mixins": [{"target": "ccf#ErrorEnvelope"}]},
            "ccf#DestinationId": {"type": "string", "traits": {"smithy.api#pattern": "^dst-[0-9]$"}},
            "ccf#ThingId": {
                "type": "string",
                "traits": {"smithy.api#pattern": "^t-[0-9]$", "smithy.api#length": {"min": 3, "max": 3}},
            },
            "ccf#ThingIdList": {"type": "list", "member": {"target": "ccf#ThingId"}},
            "ccf#Schedule": {"type": "structure", "members": {"expression": {"target": "smithy.api#String"}}},
            "ccf#Headers": {
                "type": "structure",
                "traits": {"smithy.api#mixin": {}},
                "members": {
                    "correlation_id": {"target": "smithy.api#String", "traits": {"smithy.api#httpHeader": "x-id"}}
                },
            },
            "ccf#CreateThing": {
                "type": "operation",
                "input": {"target": "ccf#CreateThingRequest"},
                "traits": {CALLERS_TRAIT: ["TENANT"]},
            },
            "ccf#CreateThingRequest": {
                "type": "structure",
                "mixins": [{"target": "ccf#Headers"}],
                "members": {
                    "destination_id": label,
                    "client_token": {"target": "smithy.api#String", "traits": {"smithy.api#idempotencyToken": {}}},
                    "schedule": {"target": "ccf#Schedule", "traits": {CLEARABLE_TRAIT: {}}},
                    "thing_ids": {"target": "ccf#ThingIdList"},
                    "parent_id": {"target": "ccf#ThingId", "traits": {"smithy.api#documentation": "The parent."}},
                },
            },
            "ccf#ListThings": {
                "type": "operation",
                "input": {"target": "ccf#ListThingsRequest"},
                "traits": {
                    CALLERS_TRAIT: ["TENANT", "RUNTIME"],
                    "smithy.api#readonly": {},
                    "smithy.api#paginated": {
                        "inputToken": "next_token",
                        "outputToken": "next_token",
                        "items": "things",
                    },
                },
            },
            "ccf#ListThingsRequest": {
                "type": "structure",
                "mixins": [{"target": "ccf#Headers"}],
                "members": {
                    "destination_id": label,
                    "next_token": {"target": "smithy.api#String", "traits": {"smithy.api#httpQuery": "next_token"}},
                },
            },
        }
    }


def _openapi() -> dict[str, Any]:
    destination = {"name": "destination_id", "in": "path", "required": True, "schema": {"type": "string"}}
    correlation = {"name": "x-id", "in": "header", "schema": {"type": "string"}}
    body = {"content": {"application/json": {"schema": {"$ref": f"{SCHEMAS}CreateThingRequestContent"}}}}
    error = {"content": {"application/json": {"schema": {"$ref": f"{SCHEMAS}BadRequestExceptionResponseContent"}}}}
    next_token = {"name": "next_token", "in": "query", "schema": {"type": "string"}}
    thing_id = {"type": "string", "pattern": "^t-[0-9]$", "minLength": 3, "maxLength": 3}
    return {
        "paths": {
            PATH: {
                "post": {
                    "operationId": "CreateThing",
                    "parameters": [destination, correlation],
                    "requestBody": body,
                    "responses": {"400": error},
                },
                "get": {"operationId": "ListThings", "parameters": [destination, next_token, correlation]},
            }
        },
        "components": {
            "schemas": {
                "CreateThingRequestContent": {
                    "type": "object",
                    "properties": {
                        "client_token": {"type": "string"},
                        "schedule": {"$ref": f"{SCHEMAS}Schedule"},
                        "thing_ids": {"type": "array", "items": thing_id},
                        "parent_id": {**thing_id, "description": "The parent."},
                    },
                },
                "Schedule": {"type": "object", "properties": {"expression": {"type": "string"}}},
                "BadRequestExceptionResponseContent": {"type": "object", "properties": {"code": {"type": "string"}}},
            }
        },
    }


def _enrich(smithy: dict[str, Any]) -> dict[str, Any]:
    openapi = _openapi()
    OpenApiEnricher(smithy, openapi).enrich()
    return openapi


def _operation(request: tuple[dict[str, Any], str]) -> dict[str, Any]:
    openapi, method = request
    return dict(openapi["paths"][PATH][method])


def test_the_complete_input_holds_body_and_parameter_members() -> None:
    schema = _enrich(_smithy())["components"]["schemas"]["CreateThingRequest"]

    assert (sorted(schema["properties"]), schema["required"]) == (
        ["client_token", "correlation_id", "destination_id", "parent_id", "schedule", "thing_ids"],
        ["destination_id"],
    )


def test_each_parameter_names_its_input_member() -> None:
    operation = _operation((_enrich(_smithy()), "get"))

    assert [parameter[Extension.MEMBER_NAME] for parameter in operation["parameters"]] == [
        "destination_id",
        "next_token",
        "correlation_id",
    ]
    assert operation[Extension.REQUEST_MODEL] == "ListThingsRequest"


def test_each_operation_carries_its_callers() -> None:
    openapi = _enrich(_smithy())

    assert [_operation((openapi, method))[Extension.CALLERS] for method in ("post", "get")] == [
        ["TENANT"],
        ["TENANT", "RUNTIME"],
    ]


def test_an_operation_without_callers_fails_the_build() -> None:
    smithy = _smithy()
    del smithy["shapes"]["ccf#CreateThing"]["traits"][CALLERS_TRAIT]

    with pytest.raises(ValueError, match="CreateThing has no @callers trait"):
        _enrich(smithy)


def test_a_runtime_operation_outside_a_destination_fails_the_build() -> None:
    smithy = _smithy()
    openapi = _openapi()
    openapi["paths"] = {"/things": openapi["paths"][PATH]}

    with pytest.raises(ValueError, match="ListThings admits RUNTIME but isn't under a destination"):
        OpenApiEnricher(smithy, openapi).enrich()


def test_a_create_names_its_client_token_member() -> None:
    openapi = _enrich(_smithy())

    assert _operation((openapi, "post"))[Extension.CLIENT_TOKEN_MEMBER] == "client_token"
    assert Extension.CLIENT_TOKEN_MEMBER not in _operation((openapi, "get"))


def test_a_list_carries_its_pagination() -> None:
    operation = _operation((_enrich(_smithy()), "get"))

    assert operation[Extension.PAGINATED] == {
        "inputToken": "next_token",
        "outputToken": "next_token",
        "items": "things",
    }


def test_only_readonly_and_idempotent_operations_are_retry_safe() -> None:
    openapi = _enrich(_smithy())

    assert [_operation((openapi, method))[Extension.RETRY_SAFE] for method in ("post", "get")] == [False, True]


def test_a_clearable_member_may_be_null() -> None:
    schemas = _enrich(_smithy())["components"]["schemas"]

    assert schemas["CreateThingRequestContent"]["properties"]["schedule"] == {
        "anyOf": [{"$ref": f"{SCHEMAS}Schedule"}, {"type": "null"}]
    }


def test_a_clearable_path_member_fails_the_build() -> None:
    smithy = _smithy()
    smithy["shapes"]["ccf#CreateThingRequest"]["members"]["destination_id"]["traits"][CLEARABLE_TRAIT] = {}

    with pytest.raises(ValueError, match="destination_id: @clearable is only for body members"):
        _enrich(smithy)


def test_a_patterned_member_references_a_schema_named_after_its_string() -> None:
    properties = _enrich(_smithy())["components"]["schemas"]["CreateThingRequestContent"]["properties"]

    assert (properties["parent_id"], properties["thing_ids"]["items"]) == (
        {"$ref": f"{SCHEMAS}ThingId", "description": "The parent."},
        {"$ref": f"{SCHEMAS}ThingId"},
    )


def test_the_named_schema_holds_the_strings_constraints() -> None:
    schemas = _enrich(_smithy())["components"]["schemas"]

    assert schemas["ThingId"] == {"type": "string", "pattern": "^t-[0-9]$", "minLength": 3, "maxLength": 3}


def test_a_patterned_member_keeps_its_smithy_default_beside_the_reference() -> None:
    smithy = _smithy()
    smithy["shapes"]["ccf#CreateThingRequest"]["members"]["parent_id"]["traits"]["smithy.api#default"] = "t-0"

    properties = _enrich(smithy)["components"]["schemas"]["CreateThingRequestContent"]["properties"]

    assert properties["parent_id"] == {"$ref": f"{SCHEMAS}ThingId", "description": "The parent.", "default": "t-0"}


def test_a_parameter_schema_stays_inline() -> None:
    operation = _operation((_enrich(_smithy()), "get"))

    assert operation["parameters"][0]["schema"] == {"type": "string"}


def test_modeled_errors_share_the_envelope_schema() -> None:
    openapi = _enrich(_smithy())
    response = _operation((openapi, "post"))["responses"]["400"]["content"]["application/json"]["schema"]

    assert (response, "BadRequestExceptionResponseContent" in openapi["components"]["schemas"]) == (
        {"$ref": f"{SCHEMAS}ErrorEnvelope"},
        False,
    )


def test_every_object_schema_is_closed() -> None:
    schemas = _enrich(_smithy())["components"]["schemas"]

    assert all(
        schemas[name]["additionalProperties"] is False
        for name in ("CreateThingRequest", "CreateThingRequestContent", "Schedule", "ErrorEnvelope")
    )


def test_every_operation_uses_the_request_validator() -> None:
    openapi = _enrich(_smithy())

    assert [_operation((openapi, method))[Extension.REQUEST_VALIDATOR] for method in ("post", "get")] == [
        REQUEST_VALIDATOR_NAME,
        REQUEST_VALIDATOR_NAME,
    ]
    assert openapi[Extension.REQUEST_VALIDATORS] == {
        REQUEST_VALIDATOR_NAME: {"validateRequestBody": True, "validateRequestParameters": True}
    }


def test_operations_carry_only_the_enrichers_extensions() -> None:
    openapi = _enrich(_smithy())

    extensions = {key for method in ("post", "get") for key in _operation((openapi, method)) if key.startswith("x-")}

    assert extensions <= set(Extension)


def test_a_validator_rejection_returns_the_error_envelope() -> None:
    responses = _enrich(_smithy())[Extension.GATEWAY_RESPONSES]

    assert {name: json.loads(responses[name]["responseTemplates"]["application/json"]) for name in responses} == {
        name: {"code": "BAD_REQUEST", "message": BAD_REQUEST_MESSAGE, "request_id": "$context.requestId"}
        for name in GATEWAY_REJECTIONS
    }


def test_a_validator_rejection_is_a_400_that_returns_the_correlation_id() -> None:
    responses = _enrich(_smithy())[Extension.GATEWAY_RESPONSES]

    assert [(response["statusCode"], response["responseParameters"]) for response in responses.values()] == [
        (400, {"gatewayresponse.header.x-correlation-id": "method.request.header.x-correlation-id"})
    ] * len(GATEWAY_REJECTIONS)


def test_a_model_without_the_bad_request_code_fails_the_build() -> None:
    smithy = _smithy()
    smithy["shapes"]["ccf#ErrorCode"]["members"] = {"CONFLICT": {"target": "smithy.api#Unit"}}

    with pytest.raises(ValueError, match="BAD_REQUEST is not a member of ccf#ErrorCode"):
        _enrich(smithy)
