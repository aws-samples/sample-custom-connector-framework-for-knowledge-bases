"""The custom connector's secret, read through the source-access session."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest
from ccf_api_models.job_container import JobExitCode

from custom_connector_framework.runner.secret_reader import ReadSecretRequest, SecretReader, SecretUnavailableError
from custom_connector_framework.runner.source_access import OpenSourceAccessRequest, SourceAccess
from custom_connector_framework.testing import FakeSecretsManager, FakeSessions, FakeSts
from tests.unit.runner.builders import EXTERNAL_ID, JOB_ID, REGION, SECRET_ARN, SOURCE_ROLE_ARN

if TYPE_CHECKING:
    import boto3
    from mypy_boto3_sts.client import STSClient

REQUEST = OpenSourceAccessRequest(role_arn=SOURCE_ROLE_ARN, external_id=EXTERNAL_ID, job_id=JOB_ID, region=REGION)


def _session(secrets: object) -> boto3.Session:
    return SourceAccess(cast("STSClient", FakeSts()), FakeSessions({"secretsmanager": secrets})).open(REQUEST)


def test_a_secret_read_error_is_secret_unavailable() -> None:
    with pytest.raises(SecretUnavailableError):
        SecretReader().read(ReadSecretRequest(source_session=_session(FakeSecretsManager()), secret_arn=SECRET_ARN))


class BinarySecretsManager:
    """`GetSecretValue` for a secret stored as binary, which has no `SecretString`."""

    def get_secret_value(self, **arguments: object) -> dict[str, object]:
        return {"ARN": arguments["SecretId"], "SecretBinary": b"\x00token"}


def test_a_binary_secret_is_secret_unavailable_not_a_crash() -> None:
    with pytest.raises(SecretUnavailableError) as raised:
        SecretReader().read(ReadSecretRequest(source_session=_session(BinarySecretsManager()), secret_arn=SECRET_ARN))

    assert raised.value.exit_code is JobExitCode.SECRET_UNAVAILABLE
