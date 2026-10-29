"""Tests for the generated typed CCF surface and its private SigV4 core."""

import inspect
import json
import re
from collections.abc import Callable
from http import HTTPStatus
from pathlib import Path
from types import NoneType
from typing import Any, cast, get_type_hints

import boto3
import pytest
from botocore.credentials import Credentials, ReadOnlyCredentials
from ccf_api_models.base import CcfModel
from ccf_api_models.models import (
    BatchDeleteCustomConnectorDocumentsRequest,
    DeleteCustomConnectorRequest,
    DocumentId,
    ErrorCode,
    GetCustomConnectorRequest,
    ListCustomConnectorsRequest,
    ListCustomConnectorsResponseContent,
    PutCustomConnectorCheckpointRequest,
)
from pydantic import SecretStr, ValidationError

import custom_connector_framework
import custom_connector_framework.client
from custom_connector_framework._generated_client import GENERATED_OPERATIONS, _Operation
from custom_connector_framework.client import CcfClient, CcfServiceError, _create_runtime_client

_ENDPOINT = "https://ccf.example.test/stage"
_OPENAPI = Path(__file__).parents[2] / "api" / "generated" / "openapi.json"
_REGION = "us-east-1"
_REQUEST_PAGE_MARKER = "page 2&more"
_RESPONSE_PAGE_MARKER = "page-3"


class _FakeHttpResponse:
    def __init__(self, status_code: int, content: bytes = b"") -> None:
        self.status_code = status_code
        self.content = content


class _CapturingHttpSession:
    def __init__(self, *responses: _FakeHttpResponse) -> None:
        self.responses = list(responses)
        self.requests: list[Any] = []

    def send(self, request: Any) -> _FakeHttpResponse:
        self.requests.append(request)
        return self.responses.pop(0)


class _RotatingCredentials:
    def __init__(self) -> None:
        self.freeze_count = 0

    def get_frozen_credentials(self) -> ReadOnlyCredentials:
        self.freeze_count += 1
        return ReadOnlyCredentials("AKID", "SECRET", f"token-{self.freeze_count}")


class _FakeBotoSession:
    def __init__(self, credentials: _RotatingCredentials) -> None:
        self.credentials = credentials
        self.resolve_count = 0

    def get_credentials(self) -> _RotatingCredentials:
        self.resolve_count += 1
        return self.credentials


def _operation_id_to_python_name(operation_id: str) -> str:
    words = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", operation_id)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", words).lower()


def _operation(name: str) -> _Operation:
    return next(operation for operation in GENERATED_OPERATIONS if operation.python_name == name)


def _client(
    http: _CapturingHttpSession,
    credentials: Credentials | ReadOnlyCredentials | _RotatingCredentials | None = None,
) -> CcfClient:
    client = CcfClient(
        endpoint=_ENDPOINT,
        region=_REGION,
        credentials=credentials or Credentials("AKID", "SECRET"),
    )
    client._http = cast(Any, http)
    return client


def test_generated_inventory_matches_openapi_and_typed_client() -> None:
    openapi_document = cast(dict[str, Any], json.loads(_OPENAPI.read_text(encoding="utf-8")))
    contract_names = {
        _operation_id_to_python_name(operation_id)
        for path_item in cast(dict[str, Any], openapi_document["paths"]).values()
        if isinstance(path_item, dict)
        for operation in path_item.values()
        if isinstance(operation, dict)
        for operation_id in [operation.get("operationId")]
        if isinstance(operation_id, str)
    }
    generated_names = {operation.python_name for operation in GENERATED_OPERATIONS}
    public_methods = {
        name for name, member in inspect.getmembers(CcfClient, predicate=callable) if not name.startswith("_")
    }

    assert contract_names == generated_names == public_methods
    assert len(generated_names) == len(GENERATED_OPERATIONS)
    for operation in GENERATED_OPERATIONS:
        operation_method = getattr(CcfClient, operation.python_name)
        annotations = get_type_hints(operation_method)
        assert annotations["request"] is operation.request_model
        assert annotations["return"] is (operation.response_model or NoneType)


def test_handwritten_client_has_no_public_operation_methods() -> None:
    generated_names = {operation.python_name for operation in GENERATED_OPERATIONS}

    assert generated_names.isdisjoint(CcfClient.__dict__)


def test_query_binding_uses_generated_wire_metadata_and_parses_typed_output() -> None:
    response_body = json.dumps({"connectors": [], "next_token": _RESPONSE_PAGE_MARKER}).encode()
    http = _CapturingHttpSession(_FakeHttpResponse(HTTPStatus.OK, response_body))
    client = _client(http)

    response = client.list_custom_connectors(
        ListCustomConnectorsRequest(max_results=25, next_token=_REQUEST_PAGE_MARKER)
    )

    assert isinstance(response, ListCustomConnectorsResponseContent)
    assert response.next_token == _RESPONSE_PAGE_MARKER
    assert http.requests[0].method == "GET"
    assert http.requests[0].url == (f"{_ENDPOINT}/api/v1/custom-connectors?max_results=25&next_token=page+2%26more")
    assert http.requests[0].body is None
    assert http.requests[0].headers["Authorization"].startswith("AWS4-HMAC-SHA256")


def test_path_binding_quotes_every_reserved_character() -> None:
    values = {"connector_id": "repo/name ?#"}

    path = CcfClient._bind_path(_operation("get_custom_connector"), values)

    assert path == "/api/v1/custom-connectors/repo%2Fname%20%3F%23"
    assert values == {}


def test_body_binding_uses_generated_members_and_reveals_secret_for_wire() -> None:
    http = _CapturingHttpSession(_FakeHttpResponse(HTTPStatus.ACCEPTED))
    client = _client(http)

    assert (
        cast(Callable[[PutCustomConnectorCheckpointRequest], object], client.put_custom_connector_checkpoint)(
            PutCustomConnectorCheckpointRequest(
                connector_id="connector-1",
                checkpoint_data=SecretStr('{"repositories":{"one":{"cursor":1}}}'),
            )
        )
        is None
    )
    assert http.requests[0].method == "PUT"
    assert json.loads(http.requests[0].body) == {"checkpoint_data": '{"repositories":{"one":{"cursor":1}}}'}


def test_delete_body_binding_is_preserved() -> None:
    http = _CapturingHttpSession(_FakeHttpResponse(HTTPStatus.ACCEPTED))
    client = _client(http)

    client.batch_delete_custom_connector_documents(
        BatchDeleteCustomConnectorDocumentsRequest(
            connector_id="connector-1",
            document_ids=[DocumentId(root="github-owner-repository-issue-1")],
        )
    )

    assert http.requests[0].method == "DELETE"
    assert json.loads(http.requests[0].body) == {"document_ids": ["github-owner-repository-issue-1"]}


def test_exact_generated_success_status_is_required() -> None:
    client = _client(_CapturingHttpSession(_FakeHttpResponse(HTTPStatus.ACCEPTED)))

    with pytest.raises(RuntimeError, match="expected 204"):
        client.delete_custom_connector(DeleteCustomConnectorRequest(connector_id="connector-1"))


def test_unit_output_returns_none_for_exact_status() -> None:
    client = _client(_CapturingHttpSession(_FakeHttpResponse(HTTPStatus.NO_CONTENT)))

    assert (
        cast(Callable[[DeleteCustomConnectorRequest], object], client.delete_custom_connector)(
            DeleteCustomConnectorRequest(connector_id="connector-1")
        )
        is None
    )


def test_generated_request_is_revalidated() -> None:
    invalid = GetCustomConnectorRequest.model_construct(connector_id="invalid/path")
    client = _client(_CapturingHttpSession())

    with pytest.raises(ValidationError):
        client.get_custom_connector(invalid)


def test_generated_success_response_is_validated_without_exposing_body() -> None:
    raw_body = b'{"secret":"must-not-appear"}'
    client = _client(_CapturingHttpSession(_FakeHttpResponse(HTTPStatus.OK, raw_body)))

    with pytest.raises(RuntimeError) as caught:
        client.list_custom_connectors(ListCustomConnectorsRequest(max_results=None, next_token=None))

    assert "invalid success response" in str(caught.value)
    assert "must-not-appear" not in str(caught.value)
    assert "secret" not in str(caught.value)
    assert caught.value.__cause__ is None


def test_modeled_error_raises_sanitized_public_service_error() -> None:
    error_body = {
        "code": "RESOURCE_NOT_FOUND",
        "message": "The requested resource was not found.",
        "request_id": "request-1",
    }
    http = _CapturingHttpSession(_FakeHttpResponse(HTTPStatus.NOT_FOUND, json.dumps(error_body).encode()))
    client = _client(http)

    with pytest.raises(CcfServiceError) as caught:
        client.get_custom_connector(GetCustomConnectorRequest(connector_id="connector-1"))

    assert caught.value.status_code == HTTPStatus.NOT_FOUND
    assert caught.value.code is ErrorCode.RESOURCE_NOT_FOUND
    assert caught.value.message == error_body["message"]
    assert caught.value.request_id == "request-1"


def test_malformed_error_body_fails_without_body_content() -> None:
    raw_body = b'{"secret":"must-not-appear"}'
    client = _client(_CapturingHttpSession(_FakeHttpResponse(HTTPStatus.BAD_REQUEST, raw_body)))

    with pytest.raises(RuntimeError) as caught:
        client.get_custom_connector(GetCustomConnectorRequest(connector_id="connector-1"))

    assert "invalid error response" in str(caught.value)
    assert "must-not-appear" not in str(caught.value)
    assert "secret" not in str(caught.value)
    assert caught.value.__cause__ is None


def test_default_boto_session_credentials_are_used(monkeypatch: pytest.MonkeyPatch) -> None:
    rotating = _RotatingCredentials()
    session = _FakeBotoSession(rotating)
    monkeypatch.setattr(boto3, "Session", lambda: session)
    http = _CapturingHttpSession(_FakeHttpResponse(HTTPStatus.OK, b'{"connectors":[]}'))
    client = CcfClient(endpoint=_ENDPOINT, region=_REGION)
    client._http = cast(Any, http)

    client.list_custom_connectors(ListCustomConnectorsRequest(max_results=None, next_token=None))

    assert session.resolve_count == 1
    assert rotating.freeze_count == 1


def test_supplied_boto_session_credentials_are_used() -> None:
    rotating = _RotatingCredentials()
    session = _FakeBotoSession(rotating)
    http = _CapturingHttpSession(_FakeHttpResponse(HTTPStatus.OK, b'{"connectors":[]}'))
    client = CcfClient(endpoint=_ENDPOINT, region=_REGION, session=cast(Any, session))
    client._http = cast(Any, http)

    client.list_custom_connectors(ListCustomConnectorsRequest(max_results=None, next_token=None))

    assert session.resolve_count == 1
    assert rotating.freeze_count == 1


def test_refreshable_credentials_are_frozen_before_each_signature() -> None:
    rotating = _RotatingCredentials()
    http = _CapturingHttpSession(
        _FakeHttpResponse(HTTPStatus.OK, b'{"connectors":[]}'),
        _FakeHttpResponse(HTTPStatus.OK, b'{"connectors":[]}'),
    )
    client = _client(http, rotating)
    request = ListCustomConnectorsRequest(max_results=None, next_token=None)

    client.list_custom_connectors(request)
    client.list_custom_connectors(request)

    assert rotating.freeze_count == 2
    assert [request.headers["X-Amz-Security-Token"] for request in http.requests] == [
        "token-1",
        "token-2",
    ]


def test_explicit_credentials_do_not_create_default_session(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_if_called() -> None:
        raise AssertionError("default session must not be created")

    monkeypatch.setattr(boto3, "Session", fail_if_called)

    CcfClient(endpoint=_ENDPOINT, region=_REGION, credentials=Credentials("AKID", "SECRET"))


def test_constructor_rejects_session_and_credentials() -> None:
    with pytest.raises(ValueError, match="not both"):
        CcfClient(
            endpoint=_ENDPOINT,
            region=_REGION,
            session=boto3.Session(region_name=_REGION),
            credentials=Credentials("AKID", "SECRET"),
        )


@pytest.mark.parametrize(
    ("endpoint", "region"),
    [
        ("http://ccf.example.test", _REGION),
        ("https://user@ccf.example.test", _REGION),
        ("https://ccf.example.test?secret=value", _REGION),
        ("https://ccf.example.test/path with space", _REGION),
        (_ENDPOINT, "not-a-region"),
    ],
)
def test_constructor_validates_endpoint_and_region(endpoint: str, region: str) -> None:
    with pytest.raises(ValueError):
        CcfClient(endpoint=endpoint, region=region, credentials=Credentials("AKID", "SECRET"))


def test_constructor_accepts_european_sovereign_cloud_region() -> None:
    CcfClient(
        endpoint=_ENDPOINT,
        region="eusc-de-east-1",
        credentials=Credentials("AKID", "SECRET"),
    )


def test_absent_query_members_are_omitted() -> None:
    http = _CapturingHttpSession(_FakeHttpResponse(HTTPStatus.OK, b'{"connectors":[]}'))
    client = _client(http)

    client.list_custom_connectors(ListCustomConnectorsRequest(max_results=None, next_token=None))

    assert http.requests[0].url == f"{_ENDPOINT}/api/v1/custom-connectors"


def test_public_surface_contains_only_one_client_and_no_transport_verbs() -> None:
    assert custom_connector_framework.__all__
    assert "CcfClient" in custom_connector_framework.__all__
    assert "CcfServiceError" in custom_connector_framework.__all__
    assert "CcfApiClient" not in custom_connector_framework.__all__
    assert "ConnectorClient" not in custom_connector_framework.__all__
    assert "StateClient" not in custom_connector_framework.__all__
    assert not {"get", "post", "put", "delete", "invoke"}.intersection(
        name for name, member in inspect.getmembers(CcfClient, predicate=callable) if not name.startswith("_")
    )


def test_generated_operation_models_are_ccf_models() -> None:
    assert all(issubclass(operation.request_model, CcfModel) for operation in GENERATED_OPERATIONS)
    assert all(
        operation.response_model is None or issubclass(operation.response_model, CcfModel)
        for operation in GENERATED_OPERATIONS
    )


def test_runtime_client_signs_the_tenant_delegation_header() -> None:
    http = _CapturingHttpSession(_FakeHttpResponse(HTTPStatus.OK, b'{"connectors":[]}'))
    client = _create_runtime_client(
        endpoint=_ENDPOINT,
        region=_REGION,
        session=cast(Any, _FakeBotoSession(_RotatingCredentials())),
        tenant_arn_prefix="arn:aws:ccf:us-east-1:123456789012",
    )
    client._http = cast(Any, http)

    client.list_custom_connectors(ListCustomConnectorsRequest(max_results=None, next_token=None))

    sent_request = http.requests[0]
    assert sent_request.headers["x-ccf-tenant-arn-prefix"] == "arn:aws:ccf:us-east-1:123456789012"
    assert "x-ccf-tenant-arn-prefix" in sent_request.headers["Authorization"]


def test_client_omits_the_delegation_header_for_client_callers() -> None:
    http = _CapturingHttpSession(_FakeHttpResponse(HTTPStatus.OK, b'{"connectors":[]}'))
    client = _client(http)

    client.list_custom_connectors(ListCustomConnectorsRequest(max_results=None, next_token=None))

    assert "x-ccf-tenant-arn-prefix" not in http.requests[0].headers


@pytest.mark.parametrize(
    "tenant_arn_prefix",
    [
        "arn:aws:ccf:us-east-1:12345678901",
        "arn:aws:ccf:us-east-1:123456789012:custom-connector/cc-1",
        "arn:aws:iam::123456789012:role/other",
        "arn:aws:ccf:us-east-1:123456789012 ",
        "",
    ],
)
def test_runtime_client_rejects_a_malformed_tenant_delegation_prefix(tenant_arn_prefix: str) -> None:
    with pytest.raises(ValueError, match="exact CCF tenant ARN prefix"):
        _create_runtime_client(
            endpoint=_ENDPOINT,
            region=_REGION,
            session=cast(Any, _FakeBotoSession(_RotatingCredentials())),
            tenant_arn_prefix=tenant_arn_prefix,
        )


def test_public_client_constructor_has_no_delegation_parameter() -> None:
    parameters = set(inspect.signature(CcfClient.__init__).parameters)

    assert "tenant_arn_prefix" not in parameters
    assert parameters == {"self", "endpoint", "region", "session", "credentials"}
    assert "_create_runtime_client" not in custom_connector_framework.client.__all__
