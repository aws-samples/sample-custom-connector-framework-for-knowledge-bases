"""Partition-aware tenant identity tests."""

import pytest

from common.tenant import TenantContext, TenantContextError


@pytest.mark.parametrize(
    ("region", "partition"),
    [
        ("us-east-1", "aws"),
        ("us-gov-west-1", "aws-us-gov"),
        ("cn-north-1", "aws-cn"),
        ("eusc-de-east-1", "aws-eusc"),
    ],
)
def test_tenant_context_accepts_partition_region_families(region: str, partition: str) -> None:
    tenant = TenantContext("123456789012", region, partition)

    assert tenant.get_arn_prefix() == f"arn:{partition}:ccf:{region}:123456789012"
    assert TenantContext.from_arn_prefix(tenant.get_arn_prefix()) == tenant


def test_tenant_context_rejects_partition_region_mismatch() -> None:
    with pytest.raises(TenantContextError, match="does not match"):
        TenantContext("123456789012", "eusc-de-east-1", "aws")
