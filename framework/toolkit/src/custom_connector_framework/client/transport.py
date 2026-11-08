"""Sends one framework operation over HTTPS: binds members, signs with SigV4, and retries.

Retries use full-jitter exponential backoff from `RetrySettings`. A 429 is retried for every
operation, because API Gateway or the framework rejected the request before running it. A 5xx or
network error is retried only for an operation the model marks `@readonly` or `@idempotent`, or one
with a client token (the caller's, or one the client fills in), because resending anything else
could do it twice. Any other 4xx is never retried. Each retry is logged at WARNING.
"""

from __future__ import annotations

import json
import logging
import random
import time
from collections.abc import Callable, Mapping
from http import HTTPStatus
from typing import Final, Protocol
from urllib.parse import quote, urlencode

from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSPreparedRequest, AWSRequest
from botocore.exceptions import ConnectionError as BotocoreConnectionError
from botocore.exceptions import HTTPClientError, NoCredentialsError
from ccf_api_models.base import ValueModel
from ccf_api_models.catalog import ERROR_STATUSES, OperationSpec, ParameterLocation
from ccf_api_models.models import ErrorCode, ErrorResponse
from pydantic import ValidationError

from custom_connector_framework.client.settings import CcfClientSettings, RetrySettings

logger = logging.getLogger(__name__)

_SIGNING_NAME = "execute-api"
_REQUEST_ID_HEADER = "x-amzn-requestid"
_CODES_BY_STATUS: Final[Mapping[int, ErrorCode]] = {int(status): code for code, status in ERROR_STATUSES.items()}
"""Keyed by the integer status, so a status `HTTPStatus` doesn't name, such as 520, falls back by class."""


class ApiFailure(ValueModel):
    """What a failed call returned: the framework's error envelope, or the code its status maps to."""

    status_code: int
    code: ErrorCode
    message: str
    request_id: str | None = None
    """The framework's or API Gateway's request ID, which finds the call in their logs; None when neither sent one."""


class CcfApiError(Exception):
    """The framework rejected or failed a call; `code` says why."""

    def __init__(self, failure: ApiFailure) -> None:
        super().__init__(
            f"{failure.code.value} ({failure.status_code}), request {failure.request_id}: {failure.message}"
        )
        self.code = failure.code
        self.status_code = failure.status_code
        self.message = failure.message
        self.request_id = failure.request_id


class OutgoingCall(ValueModel):
    """One operation's spec and its request's wire members, with the members the client filled in."""

    spec: OperationSpec
    members: dict[str, object]


class HttpResponse(Protocol):
    @property
    def status_code(self) -> int: ...

    @property
    def content(self) -> bytes: ...

    @property
    def headers(self) -> Mapping[str, str]: ...


class HttpSender(Protocol):
    def send(self, request: AWSPreparedRequest) -> HttpResponse: ...


class RetryPolicy:
    """The waits between attempts; `sleep` and `jitter` are injected so a test runs without waiting."""

    def __init__(
        self,
        settings: RetrySettings,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[float], float] = lambda ceiling: random.uniform(0, ceiling),  # noqa: S311
    ) -> None:
        self.attempts = settings.attempts
        self._settings = settings
        self._sleep = sleep
        self._jitter = jitter

    def delay(self, attempt: int) -> float:
        """Return the seconds to wait before retry number `attempt` (1 for the first retry)."""
        return self._jitter(min(self._settings.cap_seconds, self._settings.base_seconds * 2**attempt))

    def sleep(self, seconds: float) -> None:
        self._sleep(seconds)


class _HttpCall(ValueModel):
    spec: OperationSpec
    url: str
    headers: dict[str, str]
    body: bytes

    @property
    def retry_on_server_error(self) -> bool:
        """A call with a client token is resent after a server error because the token makes the resend safe."""
        return self.spec.retry_safe or self.spec.client_token_member is not None


class _Retry(ValueModel):
    call: _HttpCall
    attempt: int
    cause: str
    """The HTTP status, or the network error's type."""


class Transport:
    """Turns an outgoing call into signed HTTPS requests and returns the successful response's body."""

    def __init__(self, settings: CcfClientSettings, http: HttpSender, retry: RetryPolicy) -> None:
        self._settings = settings
        self._http = http
        self._retry = retry

    def send(self, call: OutgoingCall) -> bytes:
        """Send the call, retrying by the rules above, and return the body; raise CcfApiError on a failure."""
        return self._send(self._bound(call)).content

    def _bound(self, call: OutgoingCall) -> _HttpCall:
        """Bind the members to the path, query string, headers, and body by the spec's parameters."""
        spec = call.spec
        members = dict(call.members)
        path = spec.path
        query: list[tuple[str, str]] = []
        headers: dict[str, str] = {}
        for binding in spec.parameters:
            value = members.pop(binding.member_name, None)
            if value is None:
                continue
            match binding.location:
                case ParameterLocation.PATH:
                    path = path.replace(f"{{{binding.wire_name}}}", quote(str(value), safe=""))
                case ParameterLocation.QUERY:
                    query.append((binding.wire_name, str(value)))
                case ParameterLocation.HEADER:
                    headers[binding.wire_name] = str(value)
        body = b""
        if spec.body_members:
            values = {name: value for name, value in members.items() if name in spec.body_members}
            body = json.dumps(values, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        url = f"{self._settings.endpoint}{path}{'?' + urlencode(query) if query else ''}"
        return _HttpCall(spec=spec, url=url, headers=headers, body=body)

    def _send(self, call: _HttpCall) -> HttpResponse:
        for attempt in range(1, self._retry.attempts + 1):
            last = attempt == self._retry.attempts
            self._check_deadline(0.0)
            try:
                response = self._http.send(self._signed(call))
            except (HTTPClientError, BotocoreConnectionError) as error:
                if last or not call.retry_on_server_error:
                    raise
                self._log_retry(_Retry(call=call, attempt=attempt, cause=type(error).__name__))
                self._wait(attempt)
                continue
            if HTTPStatus.OK <= response.status_code < HTTPStatus.MULTIPLE_CHOICES:
                return response
            retryable = response.status_code == HTTPStatus.TOO_MANY_REQUESTS or (
                response.status_code >= HTTPStatus.INTERNAL_SERVER_ERROR and call.retry_on_server_error
            )
            if last or not retryable:
                raise CcfApiError(self._failure(response))
            self._log_retry(_Retry(call=call, attempt=attempt, cause=str(response.status_code)))
            self._wait(attempt)
        raise AssertionError("the retry loop always returns or raises")

    def _wait(self, attempt: int) -> None:
        """Sleep before the retry, unless the wait would end after a started send deadline."""
        seconds = self._retry.delay(attempt)
        self._check_deadline(seconds)
        self._retry.sleep(seconds)

    def _check_deadline(self, seconds_needed: float) -> None:
        deadline = self._settings.send_deadline
        if deadline is not None:
            deadline.check(seconds_needed)

    @staticmethod
    def _log_retry(retry: _Retry) -> None:
        """Log the operation, what failed, and the attempt; never the request or response body."""
        logger.warning(
            "framework_call_retried",
            extra={"operation": retry.call.spec.operation.value, "cause": retry.cause, "attempt": retry.attempt},
        )

    def _signed(self, call: _HttpCall) -> AWSPreparedRequest:
        request = AWSRequest(method=call.spec.method, url=call.url, data=call.body, headers=dict(call.headers))
        credentials = self._settings.session.get_credentials()
        if credentials is None:
            # A BotoCoreError, so the job reports it as FrameworkApiUnavailable (exit code 76).
            raise NoCredentialsError()
        SigV4Auth(credentials.get_frozen_credentials(), _SIGNING_NAME, self._settings.region).add_auth(request)
        return request.prepare()

    @staticmethod
    def _failure(response: HttpResponse) -> ApiFailure:
        """The framework's error envelope, or, for a body without it such as an API Gateway rejection, the code
        the status maps to."""
        status = response.status_code
        try:
            envelope = ErrorResponse.model_validate_json(response.content)
        except ValidationError:
            fallback = (
                ErrorCode.INTERNAL_SERVER_ERROR if status >= HTTPStatus.INTERNAL_SERVER_ERROR else ErrorCode.BAD_REQUEST
            )
            return ApiFailure(
                status_code=status,
                code=_CODES_BY_STATUS.get(status, fallback),
                message=f"The framework returned HTTP {status} without its error body",
                request_id=response.headers.get(_REQUEST_ID_HEADER) or None,
            )
        return ApiFailure(
            status_code=status, code=envelope.code, message=envelope.message, request_id=envelope.request_id
        )
