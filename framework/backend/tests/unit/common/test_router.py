"""The router's request envelope, error mapping, log levels, correlation ID, and `ApiErrors` metric."""

from __future__ import annotations

import json
import logging
import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aws_lambda_powertools.event_handler import APIGatewayRestResolver, Response
from aws_lambda_powertools.utilities.idempotency import DynamoDBPersistenceLayer, IdempotencyConfig
from aws_lambda_powertools.utilities.idempotency.exceptions import (
    IdempotencyAlreadyInProgressError,
    IdempotencyValidationError,
)
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.exceptions import ClientError
from ccf_api_models.catalog import OperationCatalog
from ccf_api_models.identifiers import Partition, TenantContext
from ccf_api_models.models import GetDestinationRequest
from ccf_api_models.operations import Operation

from common.errors import (
    DependencyThrottledError,
    DependencyUnavailableError,
    FailedAwsCall,
    ResourceNotFoundError,
    TenantRoleAccessDeniedError,
    UnauthorizedCallerError,
)
from common.idempotency import IdempotentCreates
from common.observability import MetricDimensionName, MetricName, MetricRecord
from common.operation import OperationRequest, ResolvedCaller, ResolveTenantRequest
from common.request_binder import RequestBinder
from common.router import Route, RouterAdapter

TENANT = TenantContext(partition=Partition.AWS, region="us-east-1", account_id="111122223333")
DESTINATION_ID = "dst-7c41e9a2b5d0"
SECRET_MESSAGE = "value-that-must-not-be-logged"
SPEC = OperationCatalog.load().spec(Operation.GET_DESTINATION)


def lambda_context(aws_request_id: str) -> LambdaContext:
    """The one context attribute the router reads."""
    return cast(LambdaContext, SimpleNamespace(aws_request_id=aws_request_id))


CONTEXT = lambda_context("lambda-request-1")


class FixedTenant:
    def resolve(self, request: ResolveTenantRequest) -> ResolvedCaller:
        return ResolvedCaller(tenant=TENANT)


class RecordingMetrics:
    def __init__(self) -> None:
        self.records: list[MetricRecord] = []

    def record(self, metric: MetricRecord) -> None:
        self.records.append(metric)


def raising(error: Exception) -> Any:
    def activity(request: OperationRequest[Any]) -> None:
        raise error

    return activity


def build(activity: Any) -> tuple[APIGatewayRestResolver, RecordingMetrics, list[OperationRequest[Any]]]:
    app = APIGatewayRestResolver()
    metrics = RecordingMetrics()
    idempotency = IdempotentCreates(DynamoDBPersistenceLayer(table_name="CcfIdempotency"), IdempotencyConfig())
    router = RouterAdapter(app, RequestBinder(app), FixedTenant(), idempotency, metrics)
    seen: list[OperationRequest[Any]] = []

    def recording(request: OperationRequest[Any]) -> Any:
        seen.append(request)
        return activity(request)

    @app.get("/api/v1/destinations/<destination_id>")
    def get_destination(destination_id: str) -> Response[str]:
        return router.execute(Route(spec=SPEC, activity=recording))

    return app, metrics, seen


def event(destination_id: str = DESTINATION_ID, headers: dict[str, str] | None = None) -> dict[str, Any]:
    return {
        "httpMethod": "GET",
        "path": f"/api/v1/destinations/{destination_id}",
        "resource": "/api/v1/destinations/{destination_id}",
        "pathParameters": {"destination_id": destination_id},
        "headers": headers or {},
        "queryStringParameters": None,
        "body": None,
        "requestContext": {"requestId": "request-1", "identity": {"accountId": "111122223333"}},
    }


def last_record(caplog: pytest.LogCaptureFixture) -> logging.LogRecord:
    return [record for record in caplog.records if record.name == "CustomConnectorFramework"][-1]


def correlation_header(response: dict[str, Any]) -> str:
    [value] = response["multiValueHeaders"]["x-correlation-id"]
    return str(value)


def test_the_activity_receives_the_tenant_the_payload_with_its_header_member_and_the_trace() -> None:
    app, metrics, seen = build(lambda request: None)

    response = app.resolve(event(headers={"x-ccf-connector-id": "cc-e020d65d76c0", "x-correlation-id": "c-7"}), CONTEXT)

    assert response["statusCode"] == 200
    [request] = seen
    assert request.payload == GetDestinationRequest(
        destination_id=DESTINATION_ID, connector_id="cc-e020d65d76c0", correlation_id="c-7"
    )
    assert (request.tenant, request.trace.correlation_id, request.trace.api_request_id) == (TENANT, "c-7", "request-1")
    assert correlation_header(response) == "c-7"
    assert metrics.records == []


def test_a_request_without_a_correlation_id_gets_a_new_uuid_in_the_response() -> None:
    app, _, seen = build(lambda request: None)

    response = app.resolve(event(), CONTEXT)

    uuid.UUID(correlation_header(response))
    assert seen[0].trace.correlation_id == correlation_header(response)


def test_a_malformed_path_id_is_400_and_logs_only_the_field_name(caplog: pytest.LogCaptureFixture) -> None:
    app, _, seen = build(lambda request: None)

    response = app.resolve(event(destination_id="dst-XYZ"), CONTEXT)

    warning = last_record(caplog)
    assert response["statusCode"] == 400
    assert seen == []
    assert warning.levelname == "WARNING"
    assert warning.__dict__["fields"] == ["destination_id"]
    assert "dst-XYZ" not in caplog.text
    uuid.UUID(correlation_header(response))


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (ResourceNotFoundError(SECRET_MESSAGE), 404, "RESOURCE_NOT_FOUND"),
        (IdempotencyValidationError(SECRET_MESSAGE), 409, "CONFLICT"),
        (IdempotencyAlreadyInProgressError(SECRET_MESSAGE), 409, "CONFLICT"),
        (DependencyThrottledError(SECRET_MESSAGE), 429, "THROTTLED"),
        (UnauthorizedCallerError(SECRET_MESSAGE), 403, "UNAUTHORIZED"),
    ],
)
def test_a_client_error_is_logged_at_warning_and_publishes_one_api_error(
    caplog: pytest.LogCaptureFixture, error: Exception, status: int, code: str
) -> None:
    app, metrics, _ = build(raising(error))

    response = app.resolve(event(), CONTEXT)

    warning = last_record(caplog)
    assert response["statusCode"] == status
    assert json.loads(response["body"])["code"] == code
    assert warning.levelname == "WARNING"
    assert SECRET_MESSAGE not in caplog.text
    assert [(record.name, dict(record.dimensions)) for record in metrics.records] == [
        (MetricName.API_ERRORS, {MetricDimensionName.OPERATION: "GetDestination", MetricDimensionName.ERROR_CODE: code})
    ]


@pytest.mark.parametrize(
    ("error", "status"), [(DependencyUnavailableError(SECRET_MESSAGE), 503), (RuntimeError(SECRET_MESSAGE), 500)]
)
def test_a_server_error_is_logged_at_error_with_type_and_frames_only(
    caplog: pytest.LogCaptureFixture, error: Exception, status: int
) -> None:
    app, metrics, _ = build(raising(error))

    response = app.resolve(event(), CONTEXT)

    record = last_record(caplog)
    assert response["statusCode"] == status
    assert record.levelname == "ERROR"
    assert record.__dict__["error_type"] == type(error).__name__
    assert any("in activity" in frame for frame in record.__dict__["frames"])
    assert SECRET_MESSAGE not in caplog.text
    assert SECRET_MESSAGE not in response["body"]
    assert len(metrics.records) == 1


@pytest.mark.parametrize(("code", "status"), [("ValidationException", 503), ("ThrottlingException", 429)])
def test_a_failed_aws_call_logs_its_aws_error_code_and_never_returns_it(
    caplog: pytest.LogCaptureFixture, code: str, status: int
) -> None:
    client_error = ClientError({"Error": {"Code": code, "Message": SECRET_MESSAGE}}, "UpdateItem")
    app, _, _ = build(raising(FailedAwsCall(error=client_error, action="Destination update").to_error()))

    response = app.resolve(event(), CONTEXT)

    record = last_record(caplog)
    assert response["statusCode"] == status
    assert record.__dict__["aws_error_code"] == code
    assert code not in response["body"]
    assert SECRET_MESSAGE not in caplog.text


def test_an_error_without_an_aws_call_logs_no_aws_error_code(caplog: pytest.LogCaptureFixture) -> None:
    app, _, _ = build(raising(RuntimeError(SECRET_MESSAGE)))

    app.resolve(event(), CONTEXT)

    assert not hasattr(last_record(caplog), "aws_error_code")


def test_an_unassumable_tenant_role_is_403_naming_the_role_logged_at_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    role_arn = "arn:aws:iam::111122223333:role/ccf/destinations/CcfDestinationRole-docs"
    app, _, _ = build(raising(TenantRoleAccessDeniedError(role_arn)))

    response = app.resolve(event(), CONTEXT)

    body = json.loads(response["body"])
    record = last_record(caplog)
    assert (response["statusCode"], body["code"]) == (403, "UNAUTHORIZED")
    assert role_arn in body["message"]
    assert "trust policy" in body["message"]
    assert (record.levelname, record.__dict__["error_type"]) == ("WARNING", "TenantRoleAccessDeniedError")


def test_another_403_keeps_the_generic_message_and_names_no_role() -> None:
    app, _, _ = build(raising(UnauthorizedCallerError("not this caller")))

    response = app.resolve(event(), CONTEXT)

    assert response["statusCode"] == 403
    assert json.loads(response["body"])["message"] == "The caller may not make this request"


def test_a_model_the_activity_fails_to_build_is_a_500_not_a_400(caplog: pytest.LogCaptureFixture) -> None:
    def activity(request: OperationRequest[Any]) -> None:
        GetDestinationRequest.model_validate({"destination_id": SECRET_MESSAGE})

    app, metrics, _ = build(activity)

    response = app.resolve(event(), CONTEXT)

    record = last_record(caplog)
    assert response["statusCode"] == 500
    assert json.loads(response["body"])["code"] == "INTERNAL_SERVER_ERROR"
    assert record.levelname == "ERROR"
    assert record.__dict__["error_type"] == "ValidationError"
    assert SECRET_MESSAGE not in caplog.text
    assert [metric.dimensions[MetricDimensionName.ERROR_CODE] for metric in metrics.records] == [
        "INTERNAL_SERVER_ERROR"
    ]


def test_execute_given_an_empty_api_request_id_when_it_fails_then_returns_the_lambda_request_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app, _, _ = build(lambda request: None)
    request = event(destination_id="dst-XYZ")
    request["requestContext"] = {"requestId": "", "identity": {"accountId": "111122223333"}}

    response = app.resolve(request, lambda_context("lambda-request-7"))

    assert json.loads(response["body"])["request_id"] == "lambda-request-7"
    assert "api_request_id_missing" in caplog.text
