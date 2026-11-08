"""Sessions for the roles a tenant's account trusts CCF to assume, the only way the backend reaches its resources."""

from __future__ import annotations

from typing import TYPE_CHECKING

import boto3
from botocore.exceptions import ClientError
from ccf_api_models.base import ValueModel
from ccf_api_models.identifiers import Region
from ccf_api_models.models import RoleArn

from common.aws_error_code import THROTTLING_CODES, AwsErrorCode
from common.errors import DependencyThrottledError, FailedAwsCall, TenantRoleAccessDeniedError

if TYPE_CHECKING:
    from mypy_boto3_sts import STSClient


class AssumeTenantRoleRequest(ValueModel):
    role_arn: RoleArn
    external_id: str
    """What the role's trust policy requires; the destination role requires the destination workflow's ARN."""
    session_name: str


class TenantRoleClients:
    def __init__(self, sts: STSClient, region: Region) -> None:
        self._sts = sts
        self._region = region

    def assume(self, request: AssumeTenantRoleRequest) -> boto3.Session:
        """A session with the role's credentials, in the service Region."""
        try:
            response = self._sts.assume_role(
                RoleArn=request.role_arn, RoleSessionName=request.session_name, ExternalId=request.external_id
            )
        except ClientError as error:
            code = AwsErrorCode.from_client_error(error)
            if code is AwsErrorCode.ACCESS_DENIED:
                raise TenantRoleAccessDeniedError(request.role_arn) from error
            if code in THROTTLING_CODES:
                raise DependencyThrottledError(
                    f"Throttled assuming {request.role_arn}", aws_error_code=error.response["Error"]["Code"]
                ) from error
            raise FailedAwsCall(error=error, action="Tenant role assumption").to_error() from error
        credentials = response["Credentials"]
        return boto3.Session(
            aws_access_key_id=credentials["AccessKeyId"],
            aws_secret_access_key=credentials["SecretAccessKey"],
            aws_session_token=credentials["SessionToken"],
            region_name=self._region,
        )
