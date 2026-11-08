"""The API composition root: every catalog operation has a real activity, and each route answers over the
API Gateway event shape with its status, its admitted callers, and the resolved tenant."""

from __future__ import annotations

import importlib
import inspect
import io
import json
import logging
import sys
from collections.abc import Iterator
from types import ModuleType
from typing import Any, cast

import pytest
from aws_lambda_powertools.event_handler import APIGatewayRestResolver
from aws_lambda_powertools.utilities.typing import LambdaContext as PowertoolsLambdaContext
from ccf_api_models.base import ValueModel
from ccf_api_models.catalog import OperationCatalog, OperationSpec
from ccf_api_models.identifiers import Partition, TenantContext
from ccf_api_models.operations import Operation

from common.operation import Activity, OperationRequest, ResolvedCaller, ResolveTenantRequest
from common.request_binder import RequestBinder
from common.router import RouterAdapter
from common.routes import RouteTable

TENANT = TenantContext(partition=Partition.AWS, region="us-east-1", account_id="111122223333")
PATH_IDS = {"destination_id": "dst-7c41e9a2b5d0", "connector_id": "cc-e020d65d76c0", "job_id": "ccj-0fdef5d64630"}
SPECS = OperationCatalog.load().specs()
DOCUMENT = {"document_id": "doc-0123456789abcdef", "source_document_id": "README.md", "content_hash": "sha256:9f2c"}
BODIES: dict[Operation, dict[str, Any]] = {
    Operation.CREATE_DESTINATION: {
        "name": "docs",
        "type": "S3",
        "role_arn": "arn:aws:iam::111122223333:role/ccf/destinations/CcfDestinationRole-docs",
    },
    Operation.CREATE_CUSTOM_CONNECTOR: {
        "name": "github-awslabs",
        "container_properties": {
            "image_uri": "111122223333.dkr.ecr.us-east-1.amazonaws.com/ccf-connectors/github:abc",
            "command": ["ccf-connector"],
            "resource_requirements": {"cpu": 1.0, "memory": 2048},
            "timeout": 3600,
        },
        "source_access": {"role_arn": "arn:aws:iam::111122223333:role/CcfSourceRole-github"},
    },
    Operation.UPDATE_CUSTOM_CONNECTOR: {"description": "GitHub awslabs"},
    Operation.UPDATE_DESTINATION: {"metadata_fields": []},
    Operation.START_CUSTOM_CONNECTOR_JOB: {},
    Operation.BATCH_PUT_CUSTOM_CONNECTOR_DOCUMENTS: {"documents": [DOCUMENT]},
    Operation.BATCH_DELETE_CUSTOM_CONNECTOR_DOCUMENTS: {"document_ids": [DOCUMENT["document_id"]]},
    Operation.BATCH_DELETE_CUSTOM_CONNECTOR_COLLECTIONS: {"collections": [{"collection_id": "col-default"}]},
    Operation.PUT_CUSTOM_CONNECTOR_CHECKPOINT: {"checkpoint_data": "page-42"},
}
TENANT_IDENTITY = {"accountId": "111122223333", "userArn": "arn:aws:sts::111122223333:assumed-role/Admin/session"}


class LambdaContext:
    function_name = "CcfApi"
    memory_limit_in_mb = 512
    invoked_function_arn = "arn:aws:lambda:us-east-1:444455556666:function:CcfApi"
    aws_request_id = "lambda-request-1"

    def get_remaining_time_in_millis(self) -> int:
        return 30_000


class ApiEvent(ValueModel):
    """One API Gateway proxy event: the operation's route with the path members filled in."""

    spec: OperationSpec
    path_ids: dict[str, str] = PATH_IDS
    body: dict[str, Any] | None = None
    headers: dict[str, str] = {}

    def as_event(self) -> dict[str, Any]:
        path = self.spec.path
        parameters = {name: value for name, value in self.path_ids.items() if f"{{{name}}}" in path}
        for name, value in parameters.items():
            path = path.replace(f"{{{name}}}", value)
        return {
            "httpMethod": self.spec.method.value,
            "path": path,
            "resource": self.spec.path,
            "headers": self.headers,
            "pathParameters": parameters or None,
            "queryStringParameters": None,
            "body": None if self.body is None else json.dumps(self.body),
            "isBase64Encoded": False,
            "requestContext": {"requestId": "c8f9c4b5-76e5-d76a-f980-7011e206042b", "identity": TENANT_IDENTITY},
        }


def event_of(spec: OperationSpec) -> dict[str, Any]:
    return ApiEvent(spec=spec, body=BODIES.get(spec.operation)).as_event()


class RecordingResolver:
    def __init__(self) -> None:
        self.requests: list[ResolveTenantRequest] = []

    def resolve(self, request: ResolveTenantRequest) -> ResolvedCaller:
        self.requests.append(request)
        return ResolvedCaller(tenant=TENANT)


class RecordedApi:
    """The handler's catalog, idempotency, and metrics routed to recording activities and a recording resolver."""

    def __init__(self, api: ModuleType) -> None:
        self.app = APIGatewayRestResolver()
        self.resolver = RecordingResolver()
        self.requests: dict[Operation, list[OperationRequest[Any]]] = {spec.operation: [] for spec in SPECS}
        router = RouterAdapter(self.app, RequestBinder(self.app), self.resolver, api.idempotency, api.metrics)
        RouteTable(self.app, api.catalog, router).register(
            {operation: recording(seen) for operation, seen in self.requests.items()}
        )

    def answer(self, event: dict[str, Any]) -> dict[str, Any]:
        # The fake carries only the context attributes Powertools reads.
        response: dict[str, Any] = self.app.resolve(event, cast(PowertoolsLambdaContext, LambdaContext()))
        return response


def recording(requests: list[OperationRequest[Any]]) -> Activity:
    def activity(request: OperationRequest[Any]) -> None:
        requests.append(request)

    return activity


@pytest.fixture
def api(example_environment: dict[str, str], dynamodb: Any) -> Iterator[ModuleType]:
    """The API composition root inside moto: every framework table, empty, and no client reaching AWS."""
    dynamodb.create_table(
        TableName=example_environment["TABLES__IDEMPOTENCY"],
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    sys.modules.pop("api_handler", None)
    yield importlib.import_module("api_handler")


@pytest.fixture
def recorded(api: ModuleType) -> RecordedApi:
    return RecordedApi(api)


def idempotency_records(dynamodb: Any) -> int:
    return int(dynamodb.Table("CcfIdempotency").scan(Select="COUNT")["Count"])


def test_every_activity_is_a_method_of_an_activity_or_the_shared_read_and_list_classes(api: ModuleType) -> None:
    owners = {operation: activity.__self__ for operation, activity in api.ACTIVITIES.items()}

    assert all(inspect.ismethod(activity) for activity in api.ACTIVITIES.values())
    assert {type(owner).__module__ for owner in owners.values()} - {"common.read_resources"} <= {
        f"activities.{module}"
        for module in ("checkpoints", "collections", "connectors", "destinations", "documents", "jobs")
    }


@pytest.mark.parametrize("spec", SPECS, ids=[spec.operation.value for spec in SPECS])
def test_each_operation_answers_with_its_success_status(recorded: RecordedApi, spec: OperationSpec) -> None:
    response = recorded.answer(event_of(spec))

    assert response["statusCode"] == spec.success_status, response["body"]


@pytest.mark.parametrize("spec", SPECS, ids=[spec.operation.value for spec in SPECS])
def test_each_operation_resolves_its_caller_against_the_callers_the_catalog_admits(
    recorded: RecordedApi, spec: OperationSpec
) -> None:
    recorded.answer(event_of(spec))

    assert [request.callers for request in recorded.resolver.requests] == [spec.callers]


@pytest.mark.parametrize("spec", SPECS, ids=[spec.operation.value for spec in SPECS])
def test_each_operation_runs_its_activity_once_for_the_resolved_tenant(
    recorded: RecordedApi, spec: OperationSpec
) -> None:
    recorded.answer(event_of(spec))

    assert [request.tenant for request in recorded.requests[spec.operation]] == [TENANT]


@pytest.mark.parametrize(
    "spec",
    [spec for spec in SPECS if "{destination_id}" in spec.path],
    ids=[spec.operation.value for spec in SPECS if "{destination_id}" in spec.path],
)
def test_each_destination_operation_passes_the_path_destination_id_to_its_activity(
    recorded: RecordedApi, spec: OperationSpec
) -> None:
    recorded.answer(event_of(spec))

    [request] = recorded.requests[spec.operation]
    assert request.payload.destination_id == PATH_IDS["destination_id"]


@pytest.mark.parametrize(
    ("operation", "member", "malformed"),
    [
        (Operation.GET_DESTINATION, "destination_id", "dst-XYZ"),
        (Operation.GET_CUSTOM_CONNECTOR, "connector_id", "cc-123"),
        (Operation.GET_CUSTOM_CONNECTOR_JOB, "job_id", f"ccj-{'0' * 20}"),
    ],
)
def test_a_malformed_id_in_the_path_is_400(api: ModuleType, operation: Operation, member: str, malformed: str) -> None:
    sent = ApiEvent(spec=api.catalog.spec(operation), path_ids={**PATH_IDS, member: malformed})

    response = api.handler(sent.as_event(), LambdaContext())

    assert response["statusCode"] == 400
    assert json.loads(response["body"])["code"] == "BAD_REQUEST"


def test_a_tenant_principal_on_a_runtime_only_operation_is_refused(api: ModuleType) -> None:
    spec = api.catalog.spec(Operation.BATCH_PUT_CUSTOM_CONNECTOR_DOCUMENTS)

    response = api.handler(event_of(spec), LambdaContext())

    assert response["statusCode"] == 403
    assert json.loads(response["body"])["code"] == "UNAUTHORIZED"


def test_a_tenant_principal_lists_its_destinations_through_the_real_composition(api: ModuleType) -> None:
    response = api.handler(event_of(api.catalog.spec(Operation.LIST_DESTINATIONS)), LambdaContext())

    assert response["statusCode"] == 200, response["body"]
    assert json.loads(response["body"])["destinations"] == []


def test_a_create_without_a_client_token_runs_without_an_idempotency_record(
    recorded: RecordedApi, dynamodb: Any
) -> None:
    recorded.answer(event_of(OperationCatalog.load().spec(Operation.CREATE_DESTINATION)))

    [request] = recorded.requests[Operation.CREATE_DESTINATION]
    assert request.payload.client_token is None
    assert idempotency_records(dynamodb) == 0


def test_a_get_of_a_checkpoint_none_was_stored_for_answers_200_with_an_empty_body_and_no_warning(
    api: ModuleType,
) -> None:
    stream = io.StringIO()
    written = logging.StreamHandler(stream)
    written.setFormatter(api.logger.registered_formatter)
    api.logger.addHandler(written)

    try:
        response = api.handler(event_of(api.catalog.spec(Operation.GET_CUSTOM_CONNECTOR_CHECKPOINT)), LambdaContext())
    finally:
        api.logger.removeHandler(written)

    assert (response["statusCode"], json.loads(response["body"])) == (200, {})
    assert [json.loads(line)["level"] for line in stream.getvalue().splitlines()] == ["INFO", "INFO"]


def test_every_log_line_of_a_request_carries_its_correlation_id(api: ModuleType) -> None:
    sent = ApiEvent(spec=api.catalog.spec(Operation.LIST_DESTINATIONS), headers={"x-correlation-id": "corr-1"})
    stream = io.StringIO()
    written = logging.StreamHandler(stream)
    written.setFormatter(api.logger.registered_formatter)
    api.logger.addHandler(written)

    try:
        api.handler(sent.as_event(), LambdaContext())
    finally:
        api.logger.removeHandler(written)

    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [line["message"] for line in lines] == ["api_request_received", "api_operation_succeeded"]
    assert {line.get("correlation_id") for line in lines[1:]} == {"corr-1"}


@pytest.mark.parametrize(("body", "cleared"), [({"schedule": None}, True), ({"description": "d"}, False)])
def test_an_update_passes_a_null_schedule_to_its_activity_as_sent_and_a_left_out_one_as_unsent(
    recorded: RecordedApi, body: dict[str, Any], cleared: bool
) -> None:
    spec = OperationCatalog.load().spec(Operation.UPDATE_CUSTOM_CONNECTOR)

    response = recorded.answer(ApiEvent(spec=spec, body=body).as_event())

    assert response["statusCode"] == spec.success_status, response["body"]
    [request] = recorded.requests[Operation.UPDATE_CUSTOM_CONNECTOR]
    assert ("schedule" in request.payload.model_fields_set) is cleared
    assert request.payload.schedule is None
