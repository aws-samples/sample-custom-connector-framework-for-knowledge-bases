"""Checks a `CreateDestination` request against its caller.

The model's `DestinationRoleArn` pattern fixes the role's path and name. Each destination stack
creates its `CcfDestinationRole-<name>` and the knowledge base role's boundary
`CcfDestinationBoundary-<name>` under the same path, so the boundary's ARN follows from the role's.
"""

from __future__ import annotations

from ccf_api_models.models import CreateDestinationRequest, DestinationType
from pydantic import BaseModel, ConfigDict

from common.errors import BadRequestError
from common.operation import OperationRequest

DESTINATION_ROLE_PATH = "ccf/destinations/"
DESTINATION_ROLE_PREFIX = "CcfDestinationRole-"
DESTINATION_BOUNDARY_PREFIX = "CcfDestinationBoundary-"


class DestinationRole(BaseModel):
    """A validated destination role and the knowledge base role's permissions boundary its stack created."""

    model_config = ConfigDict(frozen=True)

    role_arn: str
    boundary_arn: str


class CreateDestinationValidator:
    """Accepts a role in the caller's own account and partition, and access control only for `BEDROCK`."""

    def validate(self, request: OperationRequest[CreateDestinationRequest]) -> DestinationRole:
        payload, tenant = request.payload, request.tenant
        account_prefix = f"arn:{tenant.partition}:iam::{tenant.account_id}:"
        if not payload.role_arn.startswith(account_prefix):
            raise BadRequestError("The role isn't in the caller's account and partition", fields=("role_arn",))
        if payload.access_control_enabled and payload.type is not DestinationType.BEDROCK:
            raise BadRequestError("Only a BEDROCK destination controls access", fields=("access_control_enabled",))
        suffix = payload.role_arn.removeprefix(f"{account_prefix}role/{DESTINATION_ROLE_PATH}{DESTINATION_ROLE_PREFIX}")
        boundary_arn = f"{account_prefix}policy/{DESTINATION_ROLE_PATH}{DESTINATION_BOUNDARY_PREFIX}{suffix}"
        return DestinationRole(role_arn=payload.role_arn, boundary_arn=boundary_arn)
