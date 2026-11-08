"""The runtime role each custom connector's jobs run as, in the service account.

The role's inline policy allows only `execute-api:Invoke` on the custom connector's own
runtime-callable routes and `sts:AssumeRole` on its tenant roles (the source-access role,
and the log-delivery role when a log destination is set). Its tags name the same values,
so the shared permissions boundary can name them with policy variables. No statement
allows an S3, KMS, or IAM action.
"""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Protocol, Unpack

from botocore.exceptions import ClientError
from ccf_api_models.contracts import RuntimeRoleTagsContract
from ccf_api_models.identifiers import ConnectorRef, TenantContext
from pydantic import BaseModel, ConfigDict, StringConstraints

from common.aws_error_code import AwsErrorCode
from common.errors import FailedAwsCall

if TYPE_CHECKING:
    from mypy_boto3_iam.type_defs import (
        CreateRoleRequestTypeDef,
        DeleteRolePolicyRequestTypeDef,
        DeleteRoleRequestTypeDef,
        PutRolePolicyRequestTypeDef,
        TagRoleRequestTypeDef,
        TagTypeDef,
        UntagRoleRequestTypeDef,
    )

    from common.settings import RuntimeRoleSettings

_TAG_KEYS = RuntimeRoleTagsContract.load()
_ROLE_NAME_HASH_CHARACTERS = 32
_PLACEHOLDER = re.compile(r"\{[A-Za-z0-9_]+\}")


class RuntimeRoleTag(StrEnum):
    """The tags every runtime role carries; the permissions boundary reads them as policy variables."""

    DESTINATION_ID = _TAG_KEYS.destination_id
    CONNECTOR_ID = _TAG_KEYS.connector_id
    TENANT_ACCOUNT = _TAG_KEYS.tenant_account
    SOURCE_ACCESS_ROLE = _TAG_KEYS.source_access_role
    LOG_DELIVERY_ROLE = _TAG_KEYS.log_delivery_role


class IamRoleClient(Protocol):
    """The six IAM calls the lifecycle makes."""

    def create_role(self, **arguments: Unpack[CreateRoleRequestTypeDef]) -> object: ...
    def tag_role(self, **arguments: Unpack[TagRoleRequestTypeDef]) -> object: ...
    def untag_role(self, **arguments: Unpack[UntagRoleRequestTypeDef]) -> object: ...
    def put_role_policy(self, **arguments: Unpack[PutRolePolicyRequestTypeDef]) -> object: ...
    def delete_role_policy(self, **arguments: Unpack[DeleteRolePolicyRequestTypeDef]) -> object: ...
    def delete_role(self, **arguments: Unpack[DeleteRoleRequestTypeDef]) -> object: ...


class RuntimeRoute(BaseModel):
    """One runtime-callable route template: an API operation whose `x-ccf-callers` holds `RUNTIME`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    method: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3,7}$")]
    path: str

    def resource_for(self, connector: ConnectorRef) -> str:
        """Return `<METHOD>/<path>` with the custom connector's IDs filled in and other members as `*`."""
        bound = self.path.replace("{destination_id}", connector.destination_id).replace(
            "{connector_id}", connector.connector_id
        )
        return f"{self.method}{_PLACEHOLDER.sub('*', bound)}"


class RuntimeRoleAccess(BaseModel):
    """One custom connector and the tenant roles its runtime role may assume.

    Two accesses are equal exactly when the role's tags and policy would be the same.
    """

    model_config = ConfigDict(frozen=True)

    connector: ConnectorRef
    source_role_arn: str
    log_delivery_role_arn: str | None = None

    def tags(self) -> list[TagTypeDef]:
        connector = self.connector
        tags: list[TagTypeDef] = [
            {"Key": RuntimeRoleTag.DESTINATION_ID, "Value": connector.destination_id},
            {"Key": RuntimeRoleTag.CONNECTOR_ID, "Value": connector.connector_id},
            {"Key": RuntimeRoleTag.TENANT_ACCOUNT, "Value": connector.tenant.account_id},
            {"Key": RuntimeRoleTag.SOURCE_ACCESS_ROLE, "Value": self._path_and_name(self.source_role_arn)},
        ]
        if self.log_delivery_role_arn is not None:
            tags.append(
                {"Key": RuntimeRoleTag.LOG_DELIVERY_ROLE, "Value": self._path_and_name(self.log_delivery_role_arn)}
            )
        return tags

    def tenant_role_arns(self) -> list[str]:
        return [self.source_role_arn, *([] if self.log_delivery_role_arn is None else [self.log_delivery_role_arn])]

    @staticmethod
    def _path_and_name(role_arn: str) -> str:
        return role_arn.split(":role/", 1)[1]


class ConnectorRuntimeRoleLifecycle:
    """Creates, rewrites, and deletes one runtime role per custom connector. A lifecycle manager."""

    def __init__(self, iam: IamRoleClient, settings: RuntimeRoleSettings, service: TenantContext) -> None:
        self._iam = iam
        self._settings = settings
        self._service = service

    def role_name(self, connector: ConnectorRef) -> str:
        """Return the name prefix plus 32 hex characters of the SHA-256 of the tenant and connector."""
        identity = f"{connector.tenant.arn_prefix}:{connector.connector_id}"
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:_ROLE_NAME_HASH_CHARACTERS]
        return f"{self._settings.role_name_prefix}{digest}"

    def create(self, access: RuntimeRoleAccess) -> str:
        """Create the tagged role and its inline policy, and return the role's ARN."""
        settings = self._settings
        role_name = self.role_name(access.connector)
        try:
            self._iam.create_role(
                Path=settings.role_path,
                RoleName=role_name,
                AssumeRolePolicyDocument=self._trust_policy_document(),
                Description="CCF custom connector runtime role",
                MaxSessionDuration=settings.max_session_seconds,
                PermissionsBoundary=settings.boundary_arn,
                Tags=access.tags(),
            )
        except ClientError as error:
            if AwsErrorCode.from_client_error(error) is not AwsErrorCode.ENTITY_ALREADY_EXISTS:
                raise FailedAwsCall(error=error, action="Runtime role creation").to_error() from error
        self.replace_policy(access)
        return self._role_arn(role_name)

    def replace_policy(self, access: RuntimeRoleAccess) -> None:
        """Rewrite the role's tags and inline policy to name exactly the given tenant roles."""
        role_name = self.role_name(access.connector)
        try:
            self._iam.tag_role(RoleName=role_name, Tags=access.tags())
            if access.log_delivery_role_arn is None:
                self._iam.untag_role(RoleName=role_name, TagKeys=[RuntimeRoleTag.LOG_DELIVERY_ROLE])
            self._iam.put_role_policy(
                RoleName=role_name,
                PolicyName=self._settings.policy_name,
                PolicyDocument=self._policy_document(access),
            )
        except ClientError as error:
            raise FailedAwsCall(error=error, action="Runtime role policy update").to_error() from error

    def delete(self, connector: ConnectorRef) -> None:
        """Delete the inline policy and the role; either already gone is done."""
        role_name = self.role_name(connector)
        try:
            self._iam.delete_role_policy(RoleName=role_name, PolicyName=self._settings.policy_name)
        except ClientError as error:
            if AwsErrorCode.from_client_error(error) is not AwsErrorCode.NO_SUCH_ENTITY:
                raise FailedAwsCall(error=error, action="Runtime role policy deletion").to_error() from error
        try:
            self._iam.delete_role(RoleName=role_name)
        except ClientError as error:
            if AwsErrorCode.from_client_error(error) is not AwsErrorCode.NO_SUCH_ENTITY:
                raise FailedAwsCall(error=error, action="Runtime role deletion").to_error() from error

    def _role_arn(self, role_name: str) -> str:
        service = self._service
        return f"arn:{service.partition}:iam::{service.account_id}:role{self._settings.role_path}{role_name}"

    def _policy_document(self, access: RuntimeRoleAccess) -> str:
        """Return the runtime role identity policy for the custom connector, as JSON."""
        service, settings = self._service, self._settings
        api_prefix = (
            f"arn:{service.partition}:execute-api:{service.region}:{service.account_id}:"
            f"{settings.rest_api_id}/{settings.stage_name}/"
        )
        policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "ConnectorApiRoutes",
                    "Effect": "Allow",
                    "Action": "execute-api:Invoke",
                    "Resource": [f"{api_prefix}{route.resource_for(access.connector)}" for route in settings.routes],
                },
                {
                    "Sid": "TenantRoles",
                    "Effect": "Allow",
                    "Action": "sts:AssumeRole",
                    "Resource": access.tenant_role_arns(),
                },
            ],
        }
        return json.dumps(policy, separators=(",", ":"))

    def _trust_policy_document(self) -> str:
        service = self._service
        trust_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                    "Condition": {
                        "StringEquals": {"aws:SourceAccount": service.account_id},
                        "ArnLike": {
                            "aws:SourceArn": f"arn:{service.partition}:ecs:{service.region}:{service.account_id}:*"
                        },
                    },
                }
            ],
        }
        return json.dumps(trust_policy, separators=(",", ":"))
