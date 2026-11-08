"""The custom connector custom resource, against an in-memory framework API."""

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from ccf_api_models.base import CcfModel
from ccf_api_models.models import (
    Connector,
    CreateCustomConnectorRequest,
    CreateCustomConnectorResponseContent,
    DeleteCustomConnectorRequest,
    ErrorCode,
    GetCustomConnectorRequest,
    GetCustomConnectorResponseContent,
    UpdateCustomConnectorRequest,
    UpdateCustomConnectorResponseContent,
)
from custom_connector_framework.client import ApiFailure, CcfApiError
from jsonschema import Draft4Validator
from pydantic import BaseModel, ValidationError

import register
from custom_resource import CloudFormationRequestType, CustomResourceHandler

ROOT = Path(__file__).resolve().parents[2]
OPENAPI = json.loads((ROOT / "api/generated/openapi.json").read_text())

DESTINATION_ID = "dst-7c41e9a2b5d0"
OTHER_DESTINATION_ID = "dst-0a1b2c3d4e5f"
CONNECTOR_ID = "cc-e020d65d76c0"
STACK_ID = "arn:aws:cloudformation:us-east-1:111122223333:stack/GitHubCustomConnector/0f1e2d3c"
LOGICAL_ID = "ConnectorRegistrationDA132383"
PROVIDER_ARN = "arn:aws:lambda:us-east-1:111122223333:function:provider"
RUNTIME_ROLE_ARN = "arn:aws:iam::444455556666:role/ccf-runtime/CcfConnectorRuntime-3c9e1f0a2b7d4e6f8a1b2c3d4e5f6a7b"
EXTERNAL_ID = "ccf-ext-2f9d41c7a8b04e1f9c3d5a6b7e8f9012"
SOURCE_ROLE_ARN = "arn:aws:iam::111122223333:role/CcfSourceRole-github-awslabs"
CONNECTOR_ARN = f"arn:aws:ccf:us-east-1:111122223333:destination/{DESTINATION_ID}/custom-connector/{CONNECTOR_ID}"
BODY: dict[str, Any] = {
    "destination_id": DESTINATION_ID,
    "name": "github-awslabs",
    "container_properties": {
        "image_uri": "111122223333.dkr.ecr.us-east-1.amazonaws.com/ccf-connectors/github:0123abcd",
        "command": ["ccf-connector", "run", "github_connector:GitHubConnectorImplementation"],
        "resource_requirements": {"cpu": 2.0, "memory": 8192},
        "timeout": 43200,
    },
    "source_access": {
        "role_arn": SOURCE_ROLE_ARN,
        "secret_arn": "arn:aws:secretsmanager:us-east-1:111122223333:secret:github-token-AbCdEf",
    },
    "connector_configuration": json.dumps({"org": "awslabs"}),
}
LOG_DESTINATION = {
    "type": "CLOUDWATCH_LOGS",
    "arn": "arn:aws:logs:us-east-1:111122223333:log-group:/ccf/connectors/github-awslabs",
    "role_arn": "arn:aws:iam::111122223333:role/CcfLogDeliveryRole-github-awslabs",
}
SCHEDULE = {"expression": "cron(0 10 ? * SUN *)", "state": "ENABLED"}
CLEARABLE = {"log_destination": LOG_DESTINATION, "schedule": SCHEDULE}


def properties(members: dict[str, Any]) -> dict[str, Any]:
    """`ResourceProperties` whose request is the base body with the given members."""
    return {"ServiceToken": PROVIDER_ARN, "Request": json.dumps({**BODY, **members})}


def created_connector(destination_id: str) -> dict[str, Any]:
    return {
        "connector_id": CONNECTOR_ID,
        "arn": CONNECTOR_ARN.replace(DESTINATION_ID, destination_id),
        "destination_id": destination_id,
        "name": "github-awslabs",
        "status": "AVAILABLE",
        "created_at": 1.0,
        "updated_at": 1.0,
        "runtime_role_arn": RUNTIME_ROLE_ARN,
        "source_access": {"role_arn": SOURCE_ROLE_ARN, "external_id": EXTERNAL_ID},
    }


def not_found() -> CcfApiError:
    return CcfApiError(
        ApiFailure(status_code=404, code=ErrorCode.RESOURCE_NOT_FOUND, message="not found", request_id="req-1")
    )


class FakeCcfApi:
    """The custom connector operations `register.py` calls, recording each request."""

    def __init__(self) -> None:
        self.calls: list[CcfModel] = []
        self.exists = True

    def call(self, request: CcfModel) -> Any:
        self.calls.append(request)
        answers: dict[type[CcfModel], Callable[[Any], Any]] = {
            CreateCustomConnectorRequest: self._create,
            GetCustomConnectorRequest: self._get,
            UpdateCustomConnectorRequest: self._update,
            DeleteCustomConnectorRequest: self._delete,
        }
        return answers[type(request)](request)

    def _create(self, request: CreateCustomConnectorRequest) -> CreateCustomConnectorResponseContent:
        return CreateCustomConnectorResponseContent.model_validate_json(
            json.dumps({"connector": created_connector(request.destination_id)})
        )

    def _get(self, request: GetCustomConnectorRequest) -> GetCustomConnectorResponseContent:
        if not self.exists:
            raise not_found()
        connector = {**created_connector(request.destination_id), "container_properties": BODY["container_properties"]}
        connector["source_access"] = {**connector["source_access"], "secret_arn": BODY["source_access"]["secret_arn"]}
        return GetCustomConnectorResponseContent(connector=Connector.model_validate_json(json.dumps(connector)))

    def _update(self, request: UpdateCustomConnectorRequest) -> UpdateCustomConnectorResponseContent:
        summary = {
            key: value
            for key, value in created_connector(request.destination_id).items()
            if key not in {"runtime_role_arn", "source_access"}
        }
        return UpdateCustomConnectorResponseContent.model_validate_json(json.dumps({"connector": summary}))

    def _delete(self, request: DeleteCustomConnectorRequest) -> None:
        if not self.exists:
            raise not_found()

    def sent[RequestT: CcfModel](self, kind: type[RequestT]) -> list[RequestT]:
        return [request for request in self.calls if isinstance(request, kind)]


class Provider:
    """The handler over one fake API, as the provider framework calls it."""

    def __init__(self) -> None:
        self.api = FakeCcfApi()
        self._handler = CustomResourceHandler(register.ConnectorRegistration(self.api), register.RegistrationProperties)

    def on_event(self, event: dict[str, Any]) -> dict[str, Any]:
        return self._handler.on_event(event, None)

    def is_complete(self, event: dict[str, Any]) -> bool:
        return self._handler.is_complete(event, None)["IsComplete"] is True


def event(request_type: CloudFormationRequestType) -> dict[str, Any]:
    return {
        "RequestType": request_type.value,
        "StackId": STACK_ID,
        "LogicalResourceId": LOGICAL_ID,
        "ResourceProperties": properties({}),
    }


class PropsChange(BaseModel):
    """The request members before and after an update, on top of the base body."""

    old: dict[str, Any] = {}
    new: dict[str, Any] = {}


def update(change: PropsChange) -> dict[str, Any]:
    return {
        **event(CloudFormationRequestType.UPDATE),
        "PhysicalResourceId": CONNECTOR_ID,
        "OldResourceProperties": properties(change.old),
        "ResourceProperties": properties(change.new),
    }


def wire(request: CreateCustomConnectorRequest | UpdateCustomConnectorRequest) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(json.dumps(request.model_dump_for_wire()))
    return body


def body_errors(request: CreateCustomConnectorRequest | UpdateCustomConnectorRequest) -> list[str]:
    """Validate the body the transport sends against the schema API Gateway's request validator applies."""
    schema_name = f"{type(request).__name__.removesuffix('Request')}RequestContent"
    schema = OPENAPI["components"]["schemas"][schema_name]
    body = {name: value for name, value in wire(request).items() if name in schema["properties"]}
    validator = Draft4Validator({"$ref": f"#/components/schemas/{schema_name}", "components": OPENAPI["components"]})
    return [error.message for error in validator.iter_errors(body)]


@pytest.fixture
def provider() -> Provider:
    return Provider()


def test_a_create_makes_one_call_and_returns_the_four_attributes_from_its_response(provider: Provider) -> None:
    response = provider.on_event(event(CloudFormationRequestType.CREATE))

    assert response == {
        "PhysicalResourceId": CONNECTOR_ID,
        "Data": {
            "ConnectorId": CONNECTOR_ID,
            "ConnectorArn": CONNECTOR_ARN,
            "RuntimeRoleArn": RUNTIME_ROLE_ARN,
            "ExternalId": EXTERNAL_ID,
        },
    }
    assert [type(request) for request in provider.api.calls] == [CreateCustomConnectorRequest]


def test_a_create_sends_a_client_token_that_is_stable_across_retries(provider: Provider) -> None:
    provider.on_event(event(CloudFormationRequestType.CREATE))
    provider.on_event(event(CloudFormationRequestType.CREATE))

    tokens = {request.client_token for request in provider.api.sent(CreateCustomConnectorRequest)}
    assert tokens == {hashlib.sha256(f"{STACK_ID}\0{LOGICAL_ID}".encode()).hexdigest()}


@pytest.mark.parametrize("members", [{}, CLEARABLE])
def test_a_create_body_matches_the_api_schema(provider: Provider, members: dict[str, Any]) -> None:
    provider.on_event({**event(CloudFormationRequestType.CREATE), "ResourceProperties": properties(members)})

    [request] = provider.api.sent(CreateCustomConnectorRequest)
    assert body_errors(request) == []


def test_an_update_that_drops_the_log_destination_and_schedule_sends_both_as_null(provider: Provider) -> None:
    provider.on_event(update(PropsChange(old=CLEARABLE)))

    [request] = provider.api.sent(UpdateCustomConnectorRequest)
    assert body_errors(request) == []
    assert (wire(request)["log_destination"], wire(request)["schedule"]) == (None, None)


def test_an_update_whose_props_never_held_them_leaves_both_out(provider: Provider) -> None:
    provider.on_event(update(PropsChange()))

    [request] = provider.api.sent(UpdateCustomConnectorRequest)
    assert body_errors(request) == []
    assert {"log_destination", "schedule"}.isdisjoint(wire(request))


def test_an_update_that_keeps_them_sends_their_values(provider: Provider) -> None:
    provider.on_event(update(PropsChange(old=CLEARABLE, new=CLEARABLE)))

    [request] = provider.api.sent(UpdateCustomConnectorRequest)
    assert body_errors(request) == []
    assert wire(request)["log_destination"]["arn"] == LOG_DESTINATION["arn"]
    assert wire(request)["schedule"]["expression"] == SCHEDULE["expression"]


def test_an_update_in_place_returns_the_attributes_the_custom_connector_has(provider: Provider) -> None:
    response = provider.on_event(update(PropsChange(old={"name": "github-old"})))

    assert provider.api.sent(CreateCustomConnectorRequest) == []
    assert [request.connector_id for request in provider.api.sent(UpdateCustomConnectorRequest)] == [CONNECTOR_ID]
    assert response["Data"]["RuntimeRoleArn"] == RUNTIME_ROLE_ARN


def test_an_update_to_another_destination_creates_a_replacement_whose_token_names_the_replaced_id(
    provider: Provider,
) -> None:
    provider.on_event(update(PropsChange(new={"destination_id": OTHER_DESTINATION_ID})))

    [request] = provider.api.sent(CreateCustomConnectorRequest)
    assert request.destination_id == OTHER_DESTINATION_ID
    assert request.client_token == hashlib.sha256(f"{STACK_ID}\0{LOGICAL_ID}\0{CONNECTOR_ID}".encode()).hexdigest()
    assert provider.api.sent(UpdateCustomConnectorRequest) == []


@pytest.mark.parametrize("exists", [True, False])
def test_a_delete_succeeds_whether_or_not_the_custom_connector_still_exists(provider: Provider, exists: bool) -> None:
    provider.api.exists = exists

    response = provider.on_event({**event(CloudFormationRequestType.DELETE), "PhysicalResourceId": CONNECTOR_ID})

    assert response == {"PhysicalResourceId": CONNECTOR_ID}
    assert provider.api.sent(DeleteCustomConnectorRequest) == [
        DeleteCustomConnectorRequest(destination_id=DESTINATION_ID, connector_id=CONNECTOR_ID)
    ]


def test_a_delete_completes_only_once_the_custom_connector_is_not_found(provider: Provider) -> None:
    deleting = {**event(CloudFormationRequestType.DELETE), "PhysicalResourceId": CONNECTOR_ID}

    assert provider.is_complete(deleting) is False
    provider.api.exists = False
    assert provider.is_complete(deleting) is True


def test_an_unknown_property_fails_the_event(provider: Provider) -> None:
    misspelled = {**event(CloudFormationRequestType.CREATE), "ResourceProperties": {**properties({}), "Requst": "{}"}}

    with pytest.raises(ValidationError):
        provider.on_event(misspelled)
