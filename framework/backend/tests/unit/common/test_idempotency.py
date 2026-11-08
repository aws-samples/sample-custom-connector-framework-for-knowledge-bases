"""Client-token idempotency: saved responses, mismatches, calls in progress, failures, tenants, and expiry."""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from aws_lambda_powertools.utilities.idempotency.exceptions import (
    IdempotencyAlreadyInProgressError,
    IdempotencyValidationError,
)
from ccf_api_models.base import ValueModel
from ccf_api_models.catalog import OperationCatalog, OperationSpec
from ccf_api_models.identifiers import Partition, TenantContext
from ccf_api_models.models import CreateDestinationRequest, DestinationType, JobStatus, JobSummary
from ccf_api_models.operations import Operation
from moto import mock_aws
from pydantic import ConfigDict

from common.idempotency import IdempotentCreates, IdempotentRunRequest
from common.operation import Activity, OperationRequest, RequestTrace
from common.settings import IdempotencySettings, TableNames

TABLE = "CcfIdempotency"
EXPIRY_SECONDS = 3600
TENANT = TenantContext(partition=Partition.AWS, region="us-east-1", account_id="111122223333")
OTHER_TENANT = TenantContext(partition=Partition.AWS, region="us-east-1", account_id="444455556666")
CATALOG = OperationCatalog.load()


class Settings(ValueModel):
    tables: TableNames
    idempotency: IdempotencySettings


@pytest.fixture
def idempotency(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[IdempotentCreates, Any]]:
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName=TABLE,
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
        )
        names = {name: TABLE for name in TableNames.model_fields}
        settings = Settings(
            tables=TableNames.model_validate(names), idempotency=IdempotencySettings(expiry_seconds=EXPIRY_SECONDS)
        )
        yield IdempotentCreates.from_settings(settings), client


class CountingActivity:
    """Answers each call with a new job summary, so each call has a new job ID, and counts the calls."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, request: OperationRequest[Any]) -> JobSummary:
        self.calls += 1
        return JobSummary(
            job_id=f"ccj-{self.calls:012x}",
            arn=f"{request.tenant.arn_prefix}:job/{self.calls}",
            status=JobStatus.STARTED,
            created_at=1.0,
            updated_at=1.0,
        )


def spec(operation: Operation = Operation.CREATE_DESTINATION) -> OperationSpec:
    return CATALOG.spec(operation).model_copy(update={"response_model": JobSummary})


class Call(ValueModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    activity: Activity
    tenant: TenantContext = TENANT
    client_token: str | None = "client-token-1"
    name: str = "docs"
    correlation_id: str | None = None
    operation: Operation = Operation.CREATE_DESTINATION


def run_request(call: Call) -> IdempotentRunRequest:
    payload = CreateDestinationRequest(
        name=call.name,
        type=DestinationType.S3,
        role_arn="arn:aws:iam::111122223333:role/ccf/destinations/CcfDestinationRole-docs",
        client_token=call.client_token,
        correlation_id=call.correlation_id,
    )
    request: OperationRequest[CreateDestinationRequest] = OperationRequest(
        tenant=call.tenant, payload=payload, trace=RequestTrace(correlation_id="c-1", api_request_id="r-1")
    )
    return IdempotentRunRequest(spec=spec(call.operation), request=request, activity=call.activity)


def test_the_same_token_and_request_returns_the_saved_response(idempotency: Any) -> None:
    creates, _ = idempotency
    activity = CountingActivity()

    first = creates.run(run_request(Call(activity=activity)))
    second = creates.run(run_request(Call(activity=activity)))

    assert (second, activity.calls) == (first, 1)


def test_a_retry_with_another_correlation_id_returns_the_saved_response(idempotency: Any) -> None:
    creates, _ = idempotency
    activity = CountingActivity()

    creates.run(run_request(Call(activity=activity, correlation_id="first")))
    creates.run(run_request(Call(activity=activity, correlation_id="retry")))

    assert activity.calls == 1


def test_the_same_token_with_another_request_is_rejected(idempotency: Any) -> None:
    creates, _ = idempotency
    activity = CountingActivity()
    creates.run(run_request(Call(activity=activity)))

    with pytest.raises(IdempotencyValidationError):
        creates.run(run_request(Call(activity=activity, name="other")))


def test_a_call_while_the_first_is_running_is_rejected(idempotency: Any) -> None:
    creates, _ = idempotency
    seen: list[type[Exception]] = []
    inner = CountingActivity()

    def outer(request: OperationRequest[Any]) -> JobSummary:
        try:
            creates.run(run_request(Call(activity=inner)))
        except IdempotencyAlreadyInProgressError as error:
            seen.append(type(error))
        return inner(request)

    creates.run(run_request(Call(activity=outer)))

    assert seen == [IdempotencyAlreadyInProgressError]


def test_a_failed_first_call_lets_a_retry_create_the_resource(idempotency: Any) -> None:
    creates, _ = idempotency

    def failing(request: OperationRequest[Any]) -> JobSummary:
        raise RuntimeError("downstream failure")

    with pytest.raises(RuntimeError):
        creates.run(run_request(Call(activity=failing)))
    activity = CountingActivity()
    creates.run(run_request(Call(activity=activity)))

    assert activity.calls == 1


def test_two_tenants_with_one_token_create_two_resources(idempotency: Any) -> None:
    creates, _ = idempotency
    activity = CountingActivity()

    first = creates.run(run_request(Call(activity=activity, tenant=TENANT)))
    second = creates.run(run_request(Call(activity=activity, tenant=OTHER_TENANT)))

    assert first != second
    assert activity.calls == 2


def test_one_token_on_two_operations_runs_each_activity(idempotency: Any) -> None:
    creates, _ = idempotency
    activity = CountingActivity()

    creates.run(run_request(Call(activity=activity)))
    creates.run(run_request(Call(activity=activity, operation=Operation.CREATE_CUSTOM_CONNECTOR)))

    assert activity.calls == 2


def test_a_request_without_a_token_runs_every_time_and_writes_no_record(idempotency: Any) -> None:
    creates, client = idempotency
    activity = CountingActivity()

    creates.run(run_request(Call(activity=activity, client_token=None)))
    creates.run(run_request(Call(activity=activity, client_token=None)))

    assert activity.calls == 2
    assert client.scan(TableName=TABLE)["Count"] == 0


def test_the_record_expires_after_the_settings_expiry(idempotency: Any) -> None:
    creates, client = idempotency
    started = int(time.time())

    creates.run(run_request(Call(activity=CountingActivity())))

    expiration = int(client.scan(TableName=TABLE)["Items"][0]["expiration"]["N"])
    assert started + EXPIRY_SECONDS <= expiration <= int(time.time()) + EXPIRY_SECONDS


def test_the_stored_record_holds_no_request_value_in_plain_text(idempotency: Any) -> None:
    creates, client = idempotency

    creates.run(run_request(Call(activity=CountingActivity(), name="secret-looking-name")))

    assert "secret-looking-name" not in str(client.scan(TableName=TABLE)["Items"])
