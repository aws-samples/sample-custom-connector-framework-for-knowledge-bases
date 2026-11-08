"""A `CreateDestination` request names the caller's own role, and only `BEDROCK` turns on access control."""

from __future__ import annotations

import pytest
from ccf_api_models.identifiers import Partition, TenantContext
from ccf_api_models.models import CreateDestinationRequest, DestinationType

from common.errors import BadRequestError
from common.operation import OperationRequest, RequestTrace
from common.validators.create_destination_validator import CreateDestinationValidator

TENANT = TenantContext(partition=Partition.AWS, region="us-east-1", account_id="111122223333")
ROLE_ARN = "arn:aws:iam::111122223333:role/ccf/destinations/CcfDestinationRole-docs"
REQUEST = CreateDestinationRequest(name="docs", type=DestinationType.BEDROCK, role_arn=ROLE_ARN)
TRACE = RequestTrace(correlation_id="3f6c2d1e-8b4a-4c7e-9f10-2a5b6c7d8e9f", api_request_id="c8f9c4b5")


def validated(payload: CreateDestinationRequest) -> OperationRequest[CreateDestinationRequest]:
    return OperationRequest(tenant=TENANT, payload=payload, trace=TRACE)


@pytest.mark.parametrize(
    "role_arn",
    [
        "arn:aws:iam::444455556666:role/ccf/destinations/CcfDestinationRole-docs",
        "arn:aws-cn:iam::111122223333:role/ccf/destinations/CcfDestinationRole-docs",
    ],
    ids=["another-account", "another-partition"],
)
def test_a_role_outside_the_callers_account_or_partition_is_rejected_naming_role_arn(role_arn: str) -> None:
    with pytest.raises(BadRequestError) as raised:
        CreateDestinationValidator().validate(validated(REQUEST.model_copy(update={"role_arn": role_arn})))

    assert raised.value.fields == ("role_arn",)


@pytest.mark.parametrize("destination_type", [DestinationType.S3, DestinationType.QUICK])
def test_access_control_on_a_type_other_than_bedrock_is_rejected(destination_type: DestinationType) -> None:
    payload = REQUEST.model_copy(update={"type": destination_type, "access_control_enabled": True})

    with pytest.raises(BadRequestError) as raised:
        CreateDestinationValidator().validate(validated(payload))

    assert raised.value.fields == ("access_control_enabled",)


def test_a_valid_role_returns_the_boundary_with_the_same_suffix() -> None:
    role = CreateDestinationValidator().validate(validated(REQUEST))

    assert role.role_arn == ROLE_ARN
    assert role.boundary_arn == "arn:aws:iam::111122223333:policy/ccf/destinations/CcfDestinationBoundary-docs"
