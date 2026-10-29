"""Private SigV4 core for the generated Custom Connector Framework client."""

from __future__ import annotations

import json
import re
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, Protocol, cast
from urllib.parse import quote, urlencode, urlsplit

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials, ReadOnlyCredentials
from botocore.httpsession import URLLib3Session
from ccf_api_models.base import CcfModel
from ccf_api_models.models import ErrorCode, ErrorResponse
from pydantic import ValidationError

from custom_connector_framework._generated_client import _GeneratedCcfOperations, _Operation

if TYPE_CHECKING:
    from botocore.awsrequest import AWSPreparedRequest

_SIGNING_SERVICE = "execute-api"
_REGION_PATTERN = re.compile(r"^(?:[a-z]{2}|eusc)(?:-[a-z]+)+-[0-9]+$")
_TENANT_DELEGATION_HEADER = "x-ccf-tenant-arn-prefix"
_TENANT_ARN_PREFIX_PATTERN = re.compile(
    r"^arn:(?:aws|aws-us-gov|aws-cn|aws-eusc):ccf:(?:[a-z]{2}|eusc)(?:-[a-z]+)+-[0-9]+:[0-9]{12}$"
)


class _CredentialProvider(Protocol):
    def get_frozen_credentials(self) -> ReadOnlyCredentials:
        """Resolve credentials for one signing operation."""
        ...


class _HttpResponse(Protocol):
    status_code: int
    content: bytes


class _HttpSession(Protocol):
    def send(self, request: AWSPreparedRequest) -> _HttpResponse:
        """Send one prepared AWS request."""
        ...


class CcfServiceError(Exception):
    """A modeled CCF service error safe to expose to a caller."""

    def __init__(self, status_code: int, code: ErrorCode, message: str, request_id: str) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.request_id = request_id
        super().__init__(f"CCF request failed ({status_code}, {code.value}, request {request_id}): {message}")


class CcfClient(_GeneratedCcfOperations):
    """Call every generated CCF operation without exposing HTTP details."""

    def __init__(
        self,
        endpoint: str,
        region: str,
        session: boto3.Session | None = None,
        credentials: Credentials | ReadOnlyCredentials | _CredentialProvider | None = None,
    ) -> None:
        if session is not None and credentials is not None:
            raise ValueError("Provide either a boto3 session or botocore credentials, not both.")
        self._endpoint = self._validate_endpoint(endpoint)
        self._region = self._validate_region(region)
        self._tenant_arn_prefix: str | None = None
        self._session = session
        if session is None and credentials is None:
            self._session = boto3.Session()
        self._credentials = credentials
        self._http: _HttpSession = URLLib3Session()

    def _invoke(self, operation: _Operation, request: CcfModel) -> CcfModel | None:
        validated = operation.request_model.model_validate(request.model_dump(exclude_unset=True))
        request_values = validated.model_dump_for_wire()
        path = self._bind_path(operation, request_values)
        query = self._bind_query(operation, request_values)
        body = self._bind_body(operation, request_values)
        if request_values:
            raise RuntimeError(f"Generated client bindings are incomplete for {operation.python_name}.")
        response = self._send(operation, self._build_url(path, query), body)
        return self._parse_response(response, operation)

    def _send(self, operation: _Operation, url: str, body: bytes) -> _HttpResponse:
        headers = {"Content-Type": "application/json"} if body else {}
        if self._tenant_arn_prefix is not None:
            headers[_TENANT_DELEGATION_HEADER] = self._tenant_arn_prefix
        aws_request = AWSRequest(method=operation.method, url=url, data=body, headers=headers)
        SigV4Auth(self._resolve_credentials(), _SIGNING_SERVICE, self._region).add_auth(aws_request)
        return self._http.send(aws_request.prepare())

    def _resolve_credentials(self) -> ReadOnlyCredentials:
        credentials: _CredentialProvider | ReadOnlyCredentials | None = self._credentials
        if credentials is None and self._session is not None:
            credentials = cast(_CredentialProvider | None, self._session.get_credentials())
        if credentials is None:
            raise RuntimeError("AWS credentials are required to call CCF.")
        if isinstance(credentials, ReadOnlyCredentials):
            return credentials
        return credentials.get_frozen_credentials()

    @staticmethod
    def _bind_path(operation: _Operation, values: dict[str, Any]) -> str:
        path = operation.path
        for member_name, wire_name in operation.path_bindings:
            value = values.pop(member_name)
            path = path.replace(f"{{{wire_name}}}", quote(str(value), safe=""))
        return path

    @staticmethod
    def _bind_query(operation: _Operation, values: dict[str, Any]) -> dict[str, Any]:
        query: dict[str, Any] = {}
        for member_name, wire_name in operation.query_bindings:
            value = values.pop(member_name, None)
            if value is not None:
                query[wire_name] = value.value if hasattr(value, "value") else value
        return query

    @staticmethod
    def _bind_body(operation: _Operation, values: dict[str, Any]) -> bytes:
        body_values = {
            member_name: values.pop(member_name) for member_name in operation.body_members if member_name in values
        }
        if not body_values:
            return b""
        return json.dumps(body_values, separators=(",", ":")).encode("utf-8")

    def _build_url(self, path: str, query: dict[str, Any]) -> str:
        url = f"{self._endpoint}{path}"
        return f"{url}?{urlencode(query)}" if query else url

    @staticmethod
    def _parse_response(response: _HttpResponse, operation: _Operation) -> CcfModel | None:
        if response.status_code != operation.success_status:
            if HTTPStatus.OK <= response.status_code < HTTPStatus.MULTIPLE_CHOICES:
                raise RuntimeError(
                    f"CCF returned success status {response.status_code}; expected {operation.success_status}."
                )
            CcfClient._raise_service_error(response)
        if operation.response_model is None:
            return None
        try:
            return operation.response_model.model_validate_json(response.content)
        except ValidationError:
            raise RuntimeError(f"CCF returned an invalid success response for status {response.status_code}.") from None

    @staticmethod
    def _raise_service_error(response: _HttpResponse) -> None:
        try:
            modeled_error = ErrorResponse.model_validate_json(response.content)
        except ValidationError:
            raise RuntimeError(f"CCF returned an invalid error response for status {response.status_code}.") from None
        raise CcfServiceError(
            status_code=response.status_code,
            code=modeled_error.code,
            message=modeled_error.message,
            request_id=modeled_error.request_id,
        )

    @staticmethod
    def _validate_endpoint(endpoint: str) -> str:
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or any(character.isspace() for character in endpoint)
        ):
            raise ValueError("endpoint must be an absolute HTTPS URL without user information.")
        if parsed.query or parsed.fragment:
            raise ValueError("endpoint must not contain a query string or fragment.")
        return endpoint.rstrip("/")

    @staticmethod
    def _validate_region(region: str) -> str:
        if not _REGION_PATTERN.fullmatch(region):
            raise ValueError("region must be a valid AWS Region identifier.")
        return region


def _create_runtime_client(
    endpoint: str,
    region: str,
    session: boto3.Session,
    tenant_arn_prefix: str,
) -> CcfClient:
    """Build the framework runtime's delegating client.

    PRIVATE. Only the framework launcher may use this path. The signed tenant
    ARN-prefix header is a delegation claim, never an authorization: the control
    plane honours it only when the SigV4 caller is the exact runtime role
    persisted for the connector named in the request path.
    """
    if not _TENANT_ARN_PREFIX_PATTERN.fullmatch(tenant_arn_prefix):
        raise ValueError("tenant_arn_prefix must be an exact CCF tenant ARN prefix.")
    client = CcfClient(endpoint=endpoint, region=region, session=session)
    client._tenant_arn_prefix = tenant_arn_prefix
    return client


__all__ = ["CcfClient", "CcfServiceError"]
