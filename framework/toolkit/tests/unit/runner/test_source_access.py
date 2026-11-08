"""The source-access session: assumed with the external ID, and assumed again once its credentials expire."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

import pytest
from botocore.exceptions import ClientError
from ccf_api_models.job_container import JobExitCode

from custom_connector_framework.runner.source_access import (
    OpenSourceAccessRequest,
    SourceAccess,
    SourceAccessDeniedError,
    SourceAccessFailedError,
)
from custom_connector_framework.testing import FakeSessions, FakeSts
from tests.unit.runner.builders import EXTERNAL_ID, JOB_ID, REGION, SOURCE_ROLE_ARN

if TYPE_CHECKING:
    from mypy_boto3_sts.client import STSClient

REQUEST = OpenSourceAccessRequest(role_arn=SOURCE_ROLE_ARN, external_id=EXTERNAL_ID, job_id=JOB_ID, region=REGION)


def _source_access(sts: FakeSts) -> SourceAccess:
    return SourceAccess(cast("STSClient", sts), FakeSessions({}))


def test_credentials_past_their_expiry_are_refreshed_by_assuming_the_role_again_with_the_external_id() -> None:
    now = datetime.now(UTC)
    sts = FakeSts(now=now - timedelta(hours=2))
    session = _source_access(sts).open(REQUEST)
    sts.now = now

    credentials = session.get_credentials()
    assert credentials is not None
    frozen = credentials.get_frozen_credentials()

    assert len(sts.calls) == 2
    assert all(call["ExternalId"] == EXTERNAL_ID for call in sts.calls)
    assert all(call["RoleArn"] == SOURCE_ROLE_ARN for call in sts.calls)
    assert frozen.access_key == "ASIAFAKE000000000002"


def test_credentials_before_their_expiry_are_not_refreshed() -> None:
    sts = FakeSts()
    session = _source_access(sts).open(REQUEST)

    credentials = session.get_credentials()
    assert credentials is not None
    credentials.get_frozen_credentials()

    assert len(sts.calls) == 1


def test_the_session_is_in_the_service_region() -> None:
    assert _source_access(FakeSts()).open(REQUEST).region_name == REGION


@pytest.mark.parametrize(
    ("error_code", "error_type", "exit_code"),
    [
        ("AccessDenied", SourceAccessDeniedError, JobExitCode.SOURCE_ACCESS_DENIED),
        ("ExpiredToken", SourceAccessFailedError, JobExitCode.SOURCE_ACCESS_FAILED),
    ],
)
def test_assume_role_errors_map_to_their_exit_codes(
    error_code: str, error_type: type[Exception], exit_code: JobExitCode
) -> None:
    sts = FakeSts(error=ClientError({"Error": {"Code": error_code, "Message": "no"}}, "AssumeRole"))

    with pytest.raises(error_type) as raised:
        _source_access(sts).open(REQUEST)

    assert raised.value.exit_code is exit_code  # type: ignore[attr-defined]
