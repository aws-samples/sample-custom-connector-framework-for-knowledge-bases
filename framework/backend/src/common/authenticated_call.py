"""Who signed one API request, parsed once from the API Gateway event."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ccf_api_models.base import ValueModel
from ccf_api_models.identifiers import ACCOUNT_ID_PATTERN, PARTITION_PATTERN, Partition, TenantContext
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from common.errors import UnauthorizedCallerError

_USER_ARN = re.compile(rf"^arn:({PARTITION_PATTERN}):")
_ASSUMED_ROLE_ARN = re.compile(
    rf"^arn:(?:{PARTITION_PATTERN}):sts::({ACCOUNT_ID_PATTERN}):assumed-role/([A-Za-z0-9+=,.@_-]{{1,64}})/[^/]{{1,64}}$"
)
_IAM_ROLE_ARN = re.compile(
    rf"^arn:(?:{PARTITION_PATTERN}):iam::({ACCOUNT_ID_PATTERN}):role(?:/[A-Za-z0-9+=,.@_-]+)*/([A-Za-z0-9+=,.@_-]{{1,64}})$"
)


class RoleIdentity(ValueModel):
    """Account and name that identify exactly one IAM role."""

    account_id: str
    role_name: str

    @classmethod
    def from_assumed_role_arn(cls, assumed_role_arn: str) -> RoleIdentity:
        """Parse the STS assumed-role ARN reported for an authenticated caller."""
        match = _ASSUMED_ROLE_ARN.fullmatch(assumed_role_arn)
        if match is None:
            raise UnauthorizedCallerError("Authenticated caller is not an assumed IAM role")
        return cls(account_id=match.group(1), role_name=match.group(2))

    @classmethod
    def from_role_arn(cls, role_arn: str) -> RoleIdentity:
        """Parse a stored IAM role ARN, including a role path."""
        match = _IAM_ROLE_ARN.fullmatch(role_arn)
        if match is None:
            raise UnauthorizedCallerError("Stored runtime role ARN is malformed")
        return cls(account_id=match.group(1), role_name=match.group(2))


class _CallerIdentity(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    account_id: str = Field(alias="accountId")
    user_arn: str | None = Field(default=None, alias="userArn")


class _RequestContext(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    identity: _CallerIdentity


class AuthenticatedCall(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    request_context: _RequestContext = Field(alias="requestContext")

    @classmethod
    def from_event(cls, event: Mapping[str, Any]) -> AuthenticatedCall:
        """Parse the event, or raise UnauthorizedCallerError when the caller identity is missing or malformed."""
        try:
            return cls.model_validate(event)
        except ValidationError as error:
            raise UnauthorizedCallerError("The authenticated caller identity is malformed") from error

    @property
    def account_id(self) -> str:
        return self.request_context.identity.account_id

    def tenant(self, service: TenantContext) -> TenantContext:
        """Return the caller's tenant: its account, the service Region, and its ARN's partition or the service's."""
        identity = self.request_context.identity
        partition = service.partition
        if identity.user_arn is not None:
            match = _USER_ARN.match(identity.user_arn)
            if match is None:
                raise UnauthorizedCallerError("Authenticated caller ARN is invalid")
            partition = Partition(match.group(1))
        try:
            return TenantContext(partition=partition, region=service.region, account_id=identity.account_id)
        except ValidationError as error:
            raise UnauthorizedCallerError("Authenticated caller account is invalid") from error

    def role_identity(self) -> RoleIdentity:
        """Return the exact assumed-role identity of the caller."""
        user_arn = self.request_context.identity.user_arn
        if user_arn is None:
            raise UnauthorizedCallerError("Authenticated caller ARN is missing")
        return RoleIdentity.from_assumed_role_arn(user_arn)
