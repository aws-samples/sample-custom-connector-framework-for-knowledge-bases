"""A tenant role session carries the role's credentials, the external ID, and the service Region."""

from __future__ import annotations

from datetime import UTC, datetime

import boto3
import pytest
from botocore.stub import Stubber

from common.errors import (
    DependencyThrottledError,
    DependencyUnavailableError,
    ServiceError,
    TenantRoleAccessDeniedError,
)
from common.tenant_role_clients import AssumeTenantRoleRequest, TenantRoleClients

ROLE_ARN = "arn:aws:iam::111122223333:role/ccf/CcfDestinationRole"
ACCESS_KEY_ID = "ASIA" + "X" * 16
WORKFLOW_ARN = "arn:aws:states:us-east-1:444455556666:stateMachine:CcfDestinationWorkflow"
REQUEST = AssumeTenantRoleRequest(role_arn=ROLE_ARN, external_id=WORKFLOW_ARN, session_name="ccf-delete-cc-1")


def test_assume_sends_the_external_id_and_returns_a_session_in_the_service_region() -> None:
    sts = boto3.client("sts", region_name="us-east-1")
    stubber = Stubber(sts)
    stubber.add_response(
        "assume_role",
        {
            "Credentials": {
                "AccessKeyId": ACCESS_KEY_ID,
                "SecretAccessKey": "s" * 40,
                "SessionToken": "session-" + "token",
                "Expiration": datetime(2030, 1, 1, tzinfo=UTC),
            }
        },
        {"RoleArn": ROLE_ARN, "RoleSessionName": "ccf-delete-cc-1", "ExternalId": WORKFLOW_ARN},
    )

    with stubber:
        session = TenantRoleClients(sts, "us-west-2").assume(REQUEST)

    credentials = session.get_credentials()
    assert credentials is not None
    assert credentials.access_key == ACCESS_KEY_ID
    assert session.region_name == "us-west-2"
    stubber.assert_no_pending_responses()


def test_access_denied_names_the_role_in_the_public_message() -> None:
    sts = boto3.client("sts", region_name="us-east-1")
    stubber = Stubber(sts)
    stubber.add_client_error("assume_role", service_error_code="AccessDenied", http_status_code=403)

    with stubber, pytest.raises(TenantRoleAccessDeniedError) as raised:
        TenantRoleClients(sts, "us-east-1").assume(REQUEST)

    assert ROLE_ARN in raised.value.public_message


@pytest.mark.parametrize(
    ("code", "expected"), [("Throttling", DependencyThrottledError), ("InternalFailure", DependencyUnavailableError)]
)
def test_other_sts_errors_carry_the_aws_code(code: str, expected: type[ServiceError]) -> None:
    sts = boto3.client("sts", region_name="us-east-1")
    stubber = Stubber(sts)
    stubber.add_client_error("assume_role", service_error_code=code, http_status_code=400)

    with stubber, pytest.raises(expected) as raised:
        TenantRoleClients(sts, "us-east-1").assume(REQUEST)

    assert raised.value.aws_error_code == code
