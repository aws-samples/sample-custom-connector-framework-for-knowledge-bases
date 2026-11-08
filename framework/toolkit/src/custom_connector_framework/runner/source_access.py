"""The source-access session: the tenant's role, assumed with the external ID and refreshed before it expires.

The runtime role is itself an assumed role, so a session it assumes lasts at most an hour
(role chaining). botocore calls the refresh function again before the credentials expire, so a
job longer than an hour keeps working. The toolkit reads the secret, writes
the destination bucket, and runs the sweep through this session, and hands it to the
implementation as `JobContext.source_session`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

import boto3
from botocore.credentials import CredentialProvider, CredentialResolver, RefreshableCredentials
from botocore.exceptions import BotoCoreError, ClientError
from botocore.session import Session as BotocoreSession
from ccf_api_models.job_container import JobExitCode
from pydantic import BaseModel, ConfigDict

from custom_connector_framework.runner.aws_error_code import AwsErrorCode
from custom_connector_framework.runner.exit_codes import JobStartError

if TYPE_CHECKING:
    from mypy_boto3_sts.client import STSClient

SESSION_DURATION_SECONDS = 3600
"""The longest session a chained `AssumeRole` allows."""
_REFRESH_METHOD = "ccf-source-access"


class SourceAccessDeniedError(JobStartError):
    """`AssumeRole` on the source-access role returned `AccessDenied`."""

    exit_code = JobExitCode.SOURCE_ACCESS_DENIED


class SourceAccessFailedError(JobStartError):
    """`AssumeRole` on the source-access role failed for another reason."""

    exit_code = JobExitCode.SOURCE_ACCESS_FAILED


class OpenSourceAccessRequest(BaseModel):
    """The role to assume, its external ID, the job the session is named after, and the service Region."""

    model_config = ConfigDict(frozen=True)

    role_arn: str
    external_id: str
    job_id: str
    region: str


class _AssumedRoleProvider(CredentialProvider):
    METHOD = _REFRESH_METHOD

    def __init__(self, credentials: RefreshableCredentials) -> None:
        super().__init__()
        self._credentials = credentials

    def load(self) -> RefreshableCredentials:
        return self._credentials


class _RoleAssumer:
    """The refresh function botocore calls: one `AssumeRole` with the external ID."""

    def __init__(self, sts_client: STSClient, request: OpenSourceAccessRequest) -> None:
        self._sts = sts_client
        self._request = request

    def __call__(self) -> dict[str, str]:
        request = self._request
        credentials = self._sts.assume_role(
            RoleArn=request.role_arn,
            RoleSessionName=f"ccf-{request.job_id}",
            ExternalId=request.external_id,
            DurationSeconds=SESSION_DURATION_SECONDS,
        )["Credentials"]
        return {
            "access_key": credentials["AccessKeyId"],
            "secret_key": credentials["SecretAccessKey"],
            "token": credentials["SessionToken"],
            "expiry_time": credentials["Expiration"].isoformat(),
        }


class SessionBuilder(Protocol):
    """Builds a boto3 session over a botocore session; `boto3.Session` itself is one."""

    def __call__(self, *, botocore_session: BotocoreSession, region_name: str) -> boto3.Session: ...


class SourceAccess:
    """Opens the source-access session with the runtime role's STS client."""

    def __init__(self, sts_client: STSClient, sessions: SessionBuilder) -> None:
        self._sts = sts_client
        self._sessions = sessions

    def open(self, request: OpenSourceAccessRequest) -> boto3.Session:
        """Assume the role once now, so a denied or failed call stops the job before `run()`."""
        assume = _RoleAssumer(self._sts, request)
        try:
            metadata = assume()
        except ClientError as error:
            if AwsErrorCode.denies(error):
                raise SourceAccessDeniedError("AccessDenied assuming the source-access role") from error
            raise SourceAccessFailedError("AssumeRole failed for the source-access role") from error
        except BotoCoreError as error:
            raise SourceAccessFailedError("AssumeRole failed for the source-access role") from error
        credentials = RefreshableCredentials.create_from_metadata(
            metadata=metadata, refresh_using=assume, method=_REFRESH_METHOD
        )
        botocore_session = BotocoreSession()
        botocore_session.register_component(
            "credential_provider", CredentialResolver(providers=[_AssumedRoleProvider(credentials)])
        )
        return self._sessions(botocore_session=botocore_session, region_name=request.region)
