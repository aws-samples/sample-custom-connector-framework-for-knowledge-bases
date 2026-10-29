"""Validated tenant identity and partition-aware CCF resource names."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from common.env import AWS_REGION

_ACCOUNT_ID = re.compile(r"^[0-9]{12}$")
_REGION_PATTERN = r"(?:[a-z]{2}|eusc)(?:-[a-z]+)+-[0-9]+"
_REGION = re.compile(rf"^{_REGION_PATTERN}$")
_ARN_PREFIX = re.compile(rf"^arn:(aws|aws-us-gov|aws-cn|aws-eusc):ccf:({_REGION_PATTERN}):([0-9]{{12}})$")
_USER_ARN = re.compile(r"^arn:(aws|aws-us-gov|aws-cn|aws-eusc):")
_ASSUMED_ROLE_ARN = re.compile(
    r"^arn:(?:aws|aws-us-gov|aws-cn|aws-eusc):sts::([0-9]{12}):assumed-role/"
    r"([A-Za-z0-9+=,.@_-]{1,64})/[^/]{1,64}$"
)
_IAM_ROLE_ARN = re.compile(
    r"^arn:(?:aws|aws-us-gov|aws-cn|aws-eusc):iam::([0-9]{12}):role"
    r"(?:/[A-Za-z0-9+=,.@_-]+)*/([A-Za-z0-9+=,.@_-]{1,64})$"
)


class TenantContextError(ValueError):
    """Raised when authenticated tenant identity is absent or malformed."""


@dataclass(frozen=True)
class RoleIdentity:
    """Account and name that identify exactly one IAM role."""

    account_id: str
    role_name: str

    @classmethod
    def from_assumed_role_arn(cls, assumed_role_arn: str) -> RoleIdentity:
        """Parse the STS assumed-role ARN reported for an authenticated caller."""
        match = _ASSUMED_ROLE_ARN.fullmatch(assumed_role_arn)
        if match is None:
            raise TenantContextError("Authenticated caller is not an assumed IAM role")
        return cls(account_id=match.group(1), role_name=match.group(2))

    @classmethod
    def from_role_arn(cls, role_arn: str) -> RoleIdentity:
        """Parse a service-persisted IAM role ARN, including a role path."""
        match = _IAM_ROLE_ARN.fullmatch(role_arn)
        if match is None:
            raise TenantContextError("Stored runtime role ARN is malformed")
        return cls(account_id=match.group(1), role_name=match.group(2))


@dataclass(frozen=True)
class TenantContext:
    """Authenticated account, Region, and AWS partition."""

    account_id: str
    region: str
    partition: str | None = None

    def __post_init__(self) -> None:
        if _ACCOUNT_ID.fullmatch(self.account_id) is None:
            raise TenantContextError("Caller account must be an exact 12-digit identifier")
        if _REGION.fullmatch(self.region) is None:
            raise TenantContextError("AWS Region is invalid")
        expected_partition = self.partition or self.partition_for_region(self.region)
        if expected_partition not in {"aws", "aws-us-gov", "aws-cn", "aws-eusc"}:
            raise TenantContextError("AWS partition is invalid")
        if expected_partition != self.partition_for_region(self.region):
            raise TenantContextError("AWS partition does not match the Region")
        object.__setattr__(self, "partition", expected_partition)

    @classmethod
    def from_arn_prefix(cls, arn_prefix: str) -> TenantContext:
        """Parse an exact CCF tenant ARN prefix."""
        match = _ARN_PREFIX.fullmatch(arn_prefix)
        if match is None:
            raise TenantContextError("CCF tenant ARN prefix is invalid")
        partition, region, account_id = match.groups()
        return cls(account_id=account_id, region=region, partition=partition)

    @staticmethod
    def partition_for_region(region: str) -> str:
        """Return the AWS partition that owns a Region."""
        if region.startswith("cn-"):
            return "aws-cn"
        if region.startswith("us-gov-"):
            return "aws-us-gov"
        if region.startswith("eusc-"):
            return "aws-eusc"
        return "aws"

    def get_arn_prefix(self) -> str:
        """Return the immutable tenant resource prefix."""
        return f"arn:{self.partition}:ccf:{self.region}:{self.account_id}"

    def get_connector_arn(self, connector_id: str) -> str:
        """Return a connector resource ARN."""
        return f"{self.get_arn_prefix()}:custom-connector/{connector_id}"

    def get_job_arn(self, connector_id: str, job_id: str) -> str:
        """Return a connector job resource ARN."""
        return f"{self.get_connector_arn(connector_id)}/job/{job_id}"


def _extract_identity(event: dict[str, Any]) -> dict[str, Any]:
    request_context = event.get("requestContext")
    if not isinstance(request_context, dict):
        raise TenantContextError("API Gateway request context is missing")
    identity = request_context.get("identity")
    if not isinstance(identity, dict):
        raise TenantContextError("Authenticated caller identity is missing")
    return identity


def extract_caller_role_identity(event: dict[str, Any]) -> RoleIdentity:
    """Return the exact assumed-role identity of the authenticated caller."""
    user_arn = _extract_identity(event).get("userArn")
    if not isinstance(user_arn, str):
        raise TenantContextError("Authenticated caller ARN is missing")
    return RoleIdentity.from_assumed_role_arn(user_arn)


def extract_tenant_context(event: dict[str, Any]) -> TenantContext:
    """Extract and validate authenticated API Gateway caller identity."""
    identity = _extract_identity(event)
    account_id = identity.get("accountId")
    if not isinstance(account_id, str):
        raise TenantContextError("Authenticated caller account is missing")
    partition: str | None = None
    user_arn = identity.get("userArn")
    if isinstance(user_arn, str):
        match = _USER_ARN.match(user_arn)
        if match is None:
            raise TenantContextError("Authenticated caller ARN is invalid")
        partition = match.group(1)
    return TenantContext(account_id=account_id, region=AWS_REGION, partition=partition)
