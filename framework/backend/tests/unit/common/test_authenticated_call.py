"""The signed caller, parsed once from the API Gateway event."""

from typing import Any

import pytest
from ccf_api_models.identifiers import Partition, TenantContext

from common.authenticated_call import AuthenticatedCall, RoleIdentity
from common.errors import UnauthorizedCallerError

SERVICE = TenantContext(partition=Partition.AWS_US_GOV, region="us-gov-west-1", account_id="444455556666")


def _event(user_arn: str, **members: Any) -> dict[str, Any]:
    account_id = user_arn.split(":")[4]
    return {"requestContext": {"identity": {"accountId": account_id, "userArn": user_arn}}, **members}


def test_tenant_given_a_signed_caller_when_read_then_returns_its_account_and_partition() -> None:
    call = AuthenticatedCall.from_event(_event("arn:aws-us-gov:sts::111122223333:assumed-role/Admin/session"))

    assert call.tenant(SERVICE) == TenantContext(
        partition=Partition.AWS_US_GOV, region="us-gov-west-1", account_id="111122223333"
    )


def test_tenant_given_no_caller_arn_when_read_then_takes_the_service_partition() -> None:
    call = AuthenticatedCall.from_event({"requestContext": {"identity": {"accountId": "111122223333"}}})

    assert call.tenant(SERVICE).partition is Partition.AWS_US_GOV


def test_role_identity_given_an_assumed_role_caller_when_read_then_returns_the_role_name() -> None:
    call = AuthenticatedCall.from_event(
        _event("arn:aws:sts::444455556666:assumed-role/CcfConnectorRuntime-3c9e1f0a2b7d4e6f8a1b2c3d4e5f6a7b/ccj-1")
    )

    role = call.role_identity()

    assert (role.account_id, role.role_name) == ("444455556666", "CcfConnectorRuntime-3c9e1f0a2b7d4e6f8a1b2c3d4e5f6a7b")


def test_from_event_given_no_caller_identity_when_parsed_then_raises() -> None:
    with pytest.raises(UnauthorizedCallerError):
        AuthenticatedCall.from_event({"requestContext": {}})


def test_role_identity_given_no_caller_arn_when_read_then_raises() -> None:
    call = AuthenticatedCall.from_event({"requestContext": {"identity": {"accountId": "444455556666"}}})

    with pytest.raises(UnauthorizedCallerError, match="ARN is missing"):
        call.role_identity()


def test_role_identity_given_an_iam_user_caller_when_read_then_raises() -> None:
    call = AuthenticatedCall.from_event(_event("arn:aws:iam::444455556666:user/operator"))

    with pytest.raises(UnauthorizedCallerError, match="not an assumed IAM role"):
        call.role_identity()


def test_a_stored_runtime_role_arn_with_its_path_parses_to_the_account_and_role_name() -> None:
    role = RoleIdentity.from_role_arn(
        "arn:aws:iam::444455556666:role/ccf-runtime/CcfConnectorRuntime-3c9e1f0a2b7d4e6f8a1b2c3d4e5f6a7b"
    )

    assert (role.account_id, role.role_name) == ("444455556666", "CcfConnectorRuntime-3c9e1f0a2b7d4e6f8a1b2c3d4e5f6a7b")


@pytest.mark.parametrize(
    "role_arn",
    [
        "arn:aws:iam::444455556666:user/CcfConnectorRuntime-3c9e1f0a2b7d4e6f8a1b2c3d4e5f6a7b",
        "arn:aws:iam::44445555666:role/ccf-runtime/CcfConnectorRuntime-3c9e1f0a2b7d4e6f8a1b2c3d4e5f6a7b",
        "CcfConnectorRuntime-3c9e1f0a2b7d4e6f8a1b2c3d4e5f6a7b",
    ],
)
def test_a_malformed_stored_runtime_role_arn_is_rejected(role_arn: str) -> None:
    with pytest.raises(UnauthorizedCallerError, match="Stored runtime role ARN is malformed"):
        RoleIdentity.from_role_arn(role_arn)
