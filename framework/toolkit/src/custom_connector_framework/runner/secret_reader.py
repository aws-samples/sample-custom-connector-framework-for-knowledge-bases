"""Reads the custom connector's secret through the source-access session."""

from __future__ import annotations

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from ccf_api_models.base import ValueModel
from ccf_api_models.job_container import JobExitCode
from pydantic import ConfigDict, SecretStr

from custom_connector_framework.runner.exit_codes import JobStartError


class SecretUnavailableError(JobStartError):
    """`GetSecretValue` on the custom connector's secret failed."""

    exit_code = JobExitCode.SECRET_UNAVAILABLE


class ReadSecretRequest(ValueModel):
    """The source-access session and the secret it reads."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    source_session: boto3.Session
    secret_arn: str


class SecretReader:
    """Returns a secret's string value, read with the source-access session."""

    def read(self, request: ReadSecretRequest) -> SecretStr:
        try:
            value = request.source_session.client("secretsmanager").get_secret_value(SecretId=request.secret_arn)
        except (ClientError, BotoCoreError) as error:
            raise SecretUnavailableError("GetSecretValue failed for the custom connector's secret") from error
        secret_string = value.get("SecretString")
        if secret_string is None:
            # A binary secret has no string value the job can be given.
            raise SecretUnavailableError("The custom connector's secret has no string value")
        return SecretStr(secret_string)
