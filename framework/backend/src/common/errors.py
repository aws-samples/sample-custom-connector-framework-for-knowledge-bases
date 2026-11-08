"""Every error the API returns, each with its modeled code and the message the caller sees.

A message given to the constructor is logged by type only, never returned: it may hold a value
from the request. `public_message` is what the response carries, and holds no request value.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar

from botocore.exceptions import ClientError
from ccf_api_models.base import ValueModel
from ccf_api_models.models import ErrorCode
from pydantic import ConfigDict

from common.aws_error_code import THROTTLING_CODES, AwsErrorCode


class ServiceError(Exception):
    code: ClassVar[ErrorCode]
    public_message: str
    """An instance attribute only because `TenantRoleAccessDeniedError` names the caller's role in it."""

    def __init__(self, message: str, *, aws_error_code: str | None = None, fields: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.aws_error_code = aws_error_code
        """The AWS error code behind a failed AWS call; logged, never returned."""
        self.fields = fields
        """The request members at fault; logged by name, never by value."""


class BadRequestError(ServiceError):
    code = ErrorCode.BAD_REQUEST
    public_message = "The request is invalid"


class UnauthorizedCallerError(ServiceError):
    code = ErrorCode.UNAUTHORIZED
    public_message = "The caller may not make this request"


class TenantRoleAccessDeniedError(UnauthorizedCallerError):
    """STS refused to let the framework assume a role in the tenant's account."""

    def __init__(self, role_arn: str) -> None:
        super().__init__(f"AccessDenied assuming {role_arn}")
        self.public_message = f"The service could not assume the role {role_arn}; check its trust policy"


class DestinationRoleDeniedError(UnauthorizedCallerError):
    """The destination role was assumed but lacks a permission its stack's current version grants."""

    def __init__(self, role_arn: str) -> None:
        super().__init__(f"AccessDenied as {role_arn}")
        self.public_message = (
            f"The role {role_arn} may not make this change; deploy its destination stack again to update the role"
        )


class ResourceNotFoundError(ServiceError):
    code = ErrorCode.RESOURCE_NOT_FOUND
    public_message = "The requested resource was not found"


class ResourceConflictError(ServiceError):
    code = ErrorCode.CONFLICT
    public_message = "The request conflicts with the resource state"


class CancellationReason(ValueModel):
    """Why DynamoDB canceled one action of a transaction."""

    code: AwsErrorCode | None
    """None for an action whose condition passed."""
    item: Mapping[str, object] | None = None
    """The item the failed condition saw, when the action asked for it (`ALL_OLD`); None when it doesn't exist."""


class TransactionCanceledError(ResourceConflictError):
    def __init__(self, message: str, *, reasons: tuple[CancellationReason, ...]) -> None:
        super().__init__(message)
        self.reasons = reasons
        """One per transaction action, in request order, so a DAO can raise its own error."""


class DependencyThrottledError(ServiceError):
    code = ErrorCode.THROTTLED
    public_message = "The request was throttled; retry later"


class DependencyUnavailableError(ServiceError):
    code = ErrorCode.SERVICE_UNAVAILABLE
    public_message = "A service the request needs is unavailable"


class InternalServiceError(ServiceError):
    """A failure no other error explains; the router logs the cause's type and stack frames."""

    code = ErrorCode.INTERNAL_SERVER_ERROR
    public_message = "The service could not complete the request"


class FailedAwsCall(ValueModel):
    """An AWS call that raised an error no condition explains, and the action it served."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    error: ClientError
    action: str
    """What the caller was doing, for example "Destination lookup"; it holds no stored value."""

    def to_error(self) -> DependencyThrottledError | DependencyUnavailableError:
        """Return the throttling error when AWS throttled the call, and the unavailable error otherwise."""
        codes = {AwsErrorCode.from_client_error(self.error), *AwsErrorCode.cancellation_reasons(self.error)}
        aws_error_code = self.error.response.get("Error", {}).get("Code")
        if codes & THROTTLING_CODES:
            return DependencyThrottledError(f"{self.action} was throttled", aws_error_code=aws_error_code)
        return DependencyUnavailableError(f"{self.action} failed", aws_error_code=aws_error_code)
