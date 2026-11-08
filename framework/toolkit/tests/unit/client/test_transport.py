"""The client and transport bind members, sign, fill client tokens, retry by the rules, and delegate."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import boto3
import pytest
from botocore.awsrequest import AWSPreparedRequest
from botocore.credentials import Credentials
from botocore.exceptions import EndpointConnectionError, NoCredentialsError
from ccf_api_models.base import CcfModel, ValueModel
from ccf_api_models.catalog import OperationCatalog
from ccf_api_models.models import (
    BatchDeleteCustomConnectorCollectionsRequest,
    BatchDeleteCustomConnectorDocumentsRequest,
    BatchPutCustomConnectorDocumentsRequest,
    CollectionReference,
    CreateDestinationRequest,
    DestinationType,
    DocumentRecord,
    ErrorCode,
    GetCustomConnectorRequest,
    GetDestinationRequest,
    JobStatus,
    ListCustomConnectorJobsRequest,
    UpdateCustomConnectorRequest,
)

from custom_connector_framework.client import CcfApiError, CcfClient, CcfClientSettings, RetrySettings, TenantDelegation
from custom_connector_framework.client.send_deadline import SendDeadline, SendDeadlineExceededError
from custom_connector_framework.client.transport import RetryPolicy, Transport

SESSION_TOKEN = "session-" + "token"
ENDPOINT = "https://abc123.execute-api.us-east-1.amazonaws.com/ccf"
DESTINATION_ID = "dst-7c41e9a2b5d0"
CONNECTOR_ID = "cc-e020d65d76c0"
TENANT_ARN_PREFIX = "arn:aws:ccf:us-east-1:111122223333"
ROLE_ARN = "arn:aws:iam::111122223333:role/ccf/destinations/CcfDestinationRole-docs"
CREATED = {
    "destination": {
        "destination_id": DESTINATION_ID,
        "arn": f"{TENANT_ARN_PREFIX}:destination/{DESTINATION_ID}",
        "name": "docs",
        "type": "S3",
        "access_control_enabled": False,
        "status": "CREATING_BUCKET",
        "created_at": 1.0,
        "updated_at": 1.0,
    }
}
DESTINATION = {
    "destination": {
        **CREATED["destination"],
        "status": "ACTIVE",
        "role_arn": ROLE_ARN,
        "destination_resources": {},
    }
}
GET_DESTINATION = GetDestinationRequest(destination_id=DESTINATION_ID)


class Reply(ValueModel):
    """One response the fake HTTP sender returns, or the network error it raises."""

    status_code: int = 200
    body: object = None
    network_error: bool = False
    headers: dict[str, str] = {}

    @property
    def content(self) -> bytes:
        return b"" if self.body is None else json.dumps(self.body).encode()


class FakeHttp:
    """Records each prepared request and answers with the queued replies, in order."""

    def __init__(self) -> None:
        self.requests: list[AWSPreparedRequest] = []
        self.replies: Iterator[Reply] = iter(())

    def queue(self, replies: list[Reply]) -> None:
        self.replies = iter(replies)

    def send(self, request: AWSPreparedRequest) -> Reply:
        self.requests.append(request)
        reply = next(self.replies)
        if reply.network_error:
            raise EndpointConnectionError(endpoint_url=request.url)
        return reply

    def headers(self, index: int = -1) -> dict[str, str]:
        return {name.lower(): value for name, value in self.requests[index].headers.items()}

    def body(self, index: int = -1) -> dict[str, object]:
        sent: dict[str, object] = json.loads(cast(bytes, self.requests[index].body or b"{}"))
        return sent


class FakeClock:
    """Monotonic seconds that move only when a retry wait sleeps."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def signing_session() -> boto3.Session:
    """Fixed fake temporary credentials, so every request is signed the same way."""
    return boto3.Session(
        aws_access_key_id="ASIA" + "X" * 16,
        aws_secret_access_key="s" * 40,
        aws_session_token=SESSION_TOKEN,
        region_name="us-east-1",
    )


class CredentiallessSession(boto3.Session):
    """A session that finds no credentials, as one without a role or profile does."""

    def get_credentials(self) -> Credentials | None:
        return None


class ClientUnderTest:
    """A client over a fake HTTP sender whose retry waits advance a fake clock instead of sleeping."""

    def __init__(self, settings: CcfClientSettings) -> None:
        self.http = FakeHttp()
        self.clock = FakeClock()
        self.waits: list[float] = []

        def wait(seconds: float) -> None:
            self.waits.append(seconds)
            self.clock.sleep(seconds)

        retry = RetryPolicy(settings.retry, sleep=wait, jitter=lambda ceiling: ceiling)
        self.client = CcfClient(OperationCatalog.load(), Transport(settings, self.http, retry), settings)

    def call(self, request: CcfModel) -> Any:
        return self.client.call(request)


RUNTIME_DELEGATION = TenantDelegation(tenant_arn_prefix=TENANT_ARN_PREFIX, connector_id=CONNECTOR_ID)


@pytest.fixture
def tenant() -> ClientUnderTest:
    """A client signed as a tenant principal, which sends no delegation headers."""
    return ClientUnderTest(CcfClientSettings(endpoint=ENDPOINT, region="us-east-1", session=signing_session()))


def runtime_client(correlation_id: str) -> ClientUnderTest:
    """A client a job's runtime role uses: it delegates for its custom connector and names its job."""
    return ClientUnderTest(
        CcfClientSettings(
            endpoint=ENDPOINT,
            region="us-east-1",
            session=signing_session(),
            delegation=RUNTIME_DELEGATION,
            correlation_id=correlation_id,
        )
    )


@pytest.fixture
def runtime() -> ClientUnderTest:
    return runtime_client("ccj-5b8e0d2c41f9")


class DeadlineClient(ClientUnderTest):
    """A client whose send deadline and retry waits share one fake clock."""

    def __init__(self, clock: FakeClock) -> None:
        self.deadline = SendDeadline(clock)
        super().__init__(
            CcfClientSettings(
                endpoint=ENDPOINT, region="us-east-1", session=signing_session(), send_deadline=self.deadline
            )
        )
        self.clock = clock


def create_destination(client_token: str | None = None) -> CreateDestinationRequest:
    if client_token is None:
        return CreateDestinationRequest(name="docs", type=DestinationType.S3, role_arn=ROLE_ARN)
    return CreateDestinationRequest(name="docs", type=DestinationType.S3, role_arn=ROLE_ARN, client_token=client_token)


def as_dict(model: CcfModel) -> object:
    return json.loads(model.model_dump_json(exclude_none=True))


def test_path_and_query_members_are_bound_to_the_url(tenant: ClientUnderTest) -> None:
    tenant.http.queue([Reply(body={"jobs": []})])

    tenant.call(
        ListCustomConnectorJobsRequest(
            destination_id=DESTINATION_ID, connector_id=CONNECTOR_ID, status=JobStatus.FAILED, max_results=5
        )
    )

    url = urlsplit(tenant.http.requests[0].url)
    assert tenant.http.requests[0].method == "GET"
    assert url.path == f"/ccf/api/v1/destinations/{DESTINATION_ID}/custom-connectors/{CONNECTOR_ID}/jobs"
    assert parse_qs(url.query) == {"status": ["FAILED"], "max_results": ["5"]}
    assert tenant.http.requests[0].body in (None, b"")


def test_every_call_is_signed_with_sigv4_for_execute_api(tenant: ClientUnderTest) -> None:
    tenant.http.queue([Reply(body=DESTINATION)])

    tenant.call(GET_DESTINATION)

    headers = tenant.http.headers()
    assert headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=ASIA")
    assert "/us-east-1/execute-api/aws4_request" in headers["authorization"]
    assert "x-amz-date" in headers
    assert headers["x-amz-security-token"] == SESSION_TOKEN


def test_a_create_without_a_token_gets_one(tenant: ClientUnderTest) -> None:
    tenant.http.queue([Reply(status_code=201, body=CREATED)])

    tenant.call(create_destination())

    body = tenant.http.body()
    assert len(str(body["client_token"])) == 36
    assert body["name"] == "docs"


def test_a_create_returns_the_parsed_response(tenant: ClientUnderTest) -> None:
    tenant.http.queue([Reply(status_code=201, body=CREATED)])

    response = tenant.call(create_destination())

    assert as_dict(response) == CREATED


def test_a_429_is_retried_even_for_a_create_without_a_token(tenant: ClientUnderTest) -> None:
    tenant.http.queue([Reply(status_code=429), Reply(status_code=201, body=CREATED)])

    tenant.call(create_destination())

    assert len(tenant.http.requests) == 2
    assert tenant.http.body(0)["client_token"] == tenant.http.body(1)["client_token"]
    assert tenant.waits == [0.4]


def test_a_5xx_is_retried_for_a_create_without_a_token_with_the_token_the_client_filled(
    tenant: ClientUnderTest,
) -> None:
    tenant.http.queue(
        [
            Reply(status_code=500, body={"code": "INTERNAL_SERVER_ERROR", "message": "m", "request_id": "r"}),
            Reply(status_code=201, body=CREATED),
        ]
    )

    tenant.call(create_destination())

    first, second = tenant.http.body(0), tenant.http.body(1)
    assert first["client_token"] == second["client_token"]
    assert first["client_token"]


def test_a_network_error_on_a_create_without_a_token_is_retried_with_the_token_the_client_filled(
    tenant: ClientUnderTest,
) -> None:
    tenant.http.queue([Reply(network_error=True), Reply(status_code=201, body=CREATED)])

    tenant.call(create_destination())

    assert len(tenant.http.requests) == 2
    assert tenant.http.body(0)["client_token"] == tenant.http.body(1)["client_token"]


def test_a_5xx_is_retried_for_a_create_with_the_callers_token(tenant: ClientUnderTest) -> None:
    tenant.http.queue([Reply(status_code=503), Reply(status_code=201, body=CREATED)])

    tenant.call(create_destination("my-token"))

    assert [request["client_token"] for request in (tenant.http.body(0), tenant.http.body(1))] == ["my-token"] * 2


@pytest.mark.parametrize(
    "request_model",
    [
        BatchPutCustomConnectorDocumentsRequest(
            destination_id=DESTINATION_ID,
            connector_id=CONNECTOR_ID,
            documents=[
                DocumentRecord(
                    document_id="doc-8d41e07a9c3f2b6e", source_document_id="docs/a.md", content_hash="sha256:a"
                )
            ],
        ),
        BatchDeleteCustomConnectorDocumentsRequest(
            destination_id=DESTINATION_ID, connector_id=CONNECTOR_ID, document_ids=["doc-8d41e07a9c3f2b6e"]
        ),
        BatchDeleteCustomConnectorCollectionsRequest(
            destination_id=DESTINATION_ID,
            connector_id=CONNECTOR_ID,
            collections=[CollectionReference(collection_id="col-3f9a1c2b5d7e9f01")],
        ),
    ],
    ids=["batch-put-documents", "batch-delete-documents", "batch-delete-collections"],
)
def test_a_5xx_is_retried_for_each_idempotent_batch_operation(
    runtime: ClientUnderTest, request_model: CcfModel
) -> None:
    runtime.http.queue([Reply(status_code=500), Reply(status_code=204)])

    runtime.call(request_model)

    assert len(runtime.http.requests) == 2


def test_a_network_error_is_retried_for_a_retry_safe_operation(tenant: ClientUnderTest) -> None:
    tenant.http.queue([Reply(network_error=True), Reply(body=DESTINATION)])

    tenant.call(GET_DESTINATION)

    assert len(tenant.http.requests) == 2


def test_retries_stop_after_five_attempts_with_doubling_backoff(tenant: ClientUnderTest) -> None:
    tenant.http.queue([Reply(status_code=429)] * 5)

    with pytest.raises(CcfApiError) as raised:
        tenant.call(GET_DESTINATION)

    assert raised.value.code is ErrorCode.THROTTLED
    assert len(tenant.http.requests) == 5
    assert tenant.waits == [0.4, 0.8, 1.6, 3.2]


def test_the_retry_settings_set_the_attempts_and_the_backoff() -> None:
    retry = RetrySettings(attempts=3, base_seconds=1.0, cap_seconds=3.0)
    client = ClientUnderTest(
        CcfClientSettings(endpoint=ENDPOINT, region="us-east-1", session=signing_session(), retry=retry)
    )
    client.http.queue([Reply(status_code=429)] * 3)

    with pytest.raises(CcfApiError):
        client.call(GET_DESTINATION)

    assert (len(client.http.requests), client.waits) == (3, [2.0, 3.0])


def test_any_other_4xx_is_not_retried_and_keeps_the_frameworks_error(tenant: ClientUnderTest) -> None:
    error = {"code": "CONFLICT", "message": "The request conflicts with the resource state", "request_id": "req-1"}
    tenant.http.queue([Reply(status_code=409, body=error)])

    with pytest.raises(CcfApiError) as raised:
        tenant.call(GET_DESTINATION)

    assert (raised.value.code, raised.value.request_id) == (ErrorCode.CONFLICT, "req-1")
    assert len(tenant.http.requests) == 1


def test_an_api_gateway_rejection_without_the_error_body_maps_by_status(tenant: ClientUnderTest) -> None:
    tenant.http.queue([Reply(status_code=403, body={"message": "Forbidden"}, headers={"x-amzn-requestid": "gw-1"})])

    with pytest.raises(CcfApiError) as raised:
        tenant.call(GET_DESTINATION)

    assert (raised.value.code, raised.value.request_id) == (ErrorCode.UNAUTHORIZED, "gw-1")


@pytest.mark.parametrize(("status", "code"), [(520, ErrorCode.INTERNAL_SERVER_ERROR), (418, ErrorCode.BAD_REQUEST)])
def test_a_status_http_doesnt_name_falls_back_by_class_instead_of_raising(
    tenant: ClientUnderTest, status: int, code: ErrorCode
) -> None:
    tenant.http.queue([Reply(status_code=status, body={"message": "origin error"})] * 5)

    with pytest.raises(CcfApiError) as raised:
        tenant.call(GET_DESTINATION)

    assert raised.value.code is code


def test_a_rejection_without_a_request_id_header_has_no_request_id(tenant: ClientUnderTest) -> None:
    tenant.http.queue([Reply(status_code=403, body={"message": "Forbidden"})])

    with pytest.raises(CcfApiError) as raised:
        tenant.call(GET_DESTINATION)

    assert raised.value.request_id is None


def test_a_response_that_isnt_the_modeled_one_raises_an_internal_error(tenant: ClientUnderTest) -> None:
    tenant.http.queue([Reply(body={"unexpected": True})])

    with pytest.raises(CcfApiError) as raised:
        tenant.call(GET_DESTINATION)

    assert raised.value.code is ErrorCode.INTERNAL_SERVER_ERROR


def test_a_session_without_credentials_raises_botocores_no_credentials_error() -> None:
    client = ClientUnderTest(
        CcfClientSettings(endpoint=ENDPOINT, region="us-east-1", session=CredentiallessSession(region_name="us-east-1"))
    )

    with pytest.raises(NoCredentialsError):
        client.call(GET_DESTINATION)


def test_a_delegated_client_sends_the_tenant_header_on_every_call(runtime: ClientUnderTest) -> None:
    runtime.http.queue([Reply(body=DESTINATION), Reply(status_code=204)])

    runtime.call(GET_DESTINATION)
    runtime.call(
        BatchDeleteCustomConnectorDocumentsRequest(
            destination_id=DESTINATION_ID, connector_id=CONNECTOR_ID, document_ids=["doc-8d41e07a9c3f2b6e"]
        )
    )

    assert [runtime.http.headers(index)["x-ccf-tenant-arn-prefix"] for index in (0, 1)] == [TENANT_ARN_PREFIX] * 2


def test_the_connector_id_header_goes_only_on_get_destination(runtime: ClientUnderTest) -> None:
    connector = {
        "connector_id": CONNECTOR_ID,
        "arn": f"{TENANT_ARN_PREFIX}:destination/{DESTINATION_ID}/custom-connector/{CONNECTOR_ID}",
        "destination_id": DESTINATION_ID,
        "name": "github",
        "status": "IN_USE",
        "created_at": 1.0,
        "updated_at": 1.0,
        "runtime_role_arn": (
            "arn:aws:iam::444455556666:role/ccf-runtime/CcfConnectorRuntime-3c9e1f0a2b7d4e6f8a1b2c3d4e5f6a7b"
        ),
        "container_properties": {
            "image_uri": "111122223333.dkr.ecr.us-east-1.amazonaws.com/ccf-connectors/github:1.0.0",
            "command": ["run"],
            "resource_requirements": {"cpu": 1.0, "memory": 2048},
            "timeout": 3600,
        },
        "source_access": {"role_arn": "arn:aws:iam::111122223333:role/CcfSourceRole-github", "external_id": "ccf-x"},
    }
    runtime.http.queue([Reply(body=DESTINATION), Reply(body={"connector": connector})])

    runtime.call(GET_DESTINATION)
    runtime.call(GetCustomConnectorRequest(destination_id=DESTINATION_ID, connector_id=CONNECTOR_ID))

    assert runtime.http.headers(0)["x-ccf-connector-id"] == CONNECTOR_ID
    assert "x-ccf-connector-id" not in runtime.http.headers(1)


def test_a_tenant_client_sends_no_delegation_header(tenant: ClientUnderTest) -> None:
    tenant.http.queue([Reply(body=DESTINATION)])

    tenant.call(GET_DESTINATION)

    assert {"x-ccf-tenant-arn-prefix", "x-ccf-connector-id"}.isdisjoint(tenant.http.headers())


def test_every_call_carries_the_settings_correlation_id() -> None:
    job = runtime_client("ccj-0fdef5d64630")
    job.http.queue([Reply(status_code=500), Reply(body=DESTINATION)])

    job.call(GET_DESTINATION)

    assert [job.http.headers(index).get("x-correlation-id") for index in (0, 1)] == ["ccj-0fdef5d64630"] * 2


def test_a_call_without_a_correlation_id_sends_no_header(tenant: ClientUnderTest) -> None:
    tenant.http.queue([Reply(body=DESTINATION)])

    tenant.call(GET_DESTINATION)

    assert "x-correlation-id" not in tenant.http.headers()


def test_a_retry_is_logged_once_at_warning_with_the_operation_status_and_attempt(
    tenant: ClientUnderTest, caplog: pytest.LogCaptureFixture
) -> None:
    tenant.http.queue([Reply(status_code=429), Reply(body=DESTINATION)])

    tenant.call(GET_DESTINATION)

    [retried] = [record for record in caplog.records if record.getMessage() == "framework_call_retried"]
    assert retried.levelname == "WARNING"
    assert (retried.operation, retried.cause, retried.attempt) == ("GetDestination", "429", 1)  # type: ignore[attr-defined]


def test_a_call_that_succeeds_first_logs_no_retry(tenant: ClientUnderTest, caplog: pytest.LogCaptureFixture) -> None:
    tenant.http.queue([Reply(body=DESTINATION)])

    tenant.call(GET_DESTINATION)

    assert [record for record in caplog.records if record.getMessage() == "framework_call_retried"] == []


def test_send_given_a_started_deadline_when_the_next_wait_would_pass_it_then_stops_without_waiting() -> None:
    client = DeadlineClient(FakeClock())
    client.deadline.start(1.0)
    client.http.queue([Reply(status_code=429)] * 5)

    with pytest.raises(SendDeadlineExceededError):
        client.call(GET_DESTINATION)

    assert (len(client.http.requests), client.clock.now) == (2, 0.4)


def test_send_given_a_deadline_already_passed_when_called_then_sends_nothing() -> None:
    client = DeadlineClient(FakeClock())
    client.deadline.start(0.0)
    client.clock.sleep(0.1)

    with pytest.raises(SendDeadlineExceededError):
        client.call(GET_DESTINATION)

    assert client.http.requests == []


def test_send_given_a_deadline_never_started_when_throttled_then_retries_every_attempt() -> None:
    client = DeadlineClient(FakeClock())
    client.http.queue([Reply(status_code=429)] * 5)

    with pytest.raises(CcfApiError):
        client.call(GET_DESTINATION)

    assert len(client.http.requests) == 5


def test_an_update_that_clears_the_schedule_sends_it_as_null_and_leaves_out_what_it_keeps(
    tenant: ClientUnderTest,
) -> None:
    summary = {
        "connector_id": CONNECTOR_ID,
        "arn": f"{TENANT_ARN_PREFIX}:destination/{DESTINATION_ID}/custom-connector/{CONNECTOR_ID}",
        "destination_id": DESTINATION_ID,
        "name": "github-awslabs",
        "status": "AVAILABLE",
        "created_at": 1.0,
        "updated_at": 1.0,
    }
    tenant.http.queue([Reply(body={"connector": summary})])

    tenant.call(UpdateCustomConnectorRequest(destination_id=DESTINATION_ID, connector_id=CONNECTOR_ID, schedule=None))

    assert tenant.http.body() == {"schedule": None}
