"""The source-access and log-delivery role ARNs: a role named for its use, with no path, in any partition."""

import pytest
from ccf_api_models.models import LogDeliveryRoleArn, SourceRoleArn
from pydantic import TypeAdapter, ValidationError

SOURCE_ROLE = TypeAdapter(SourceRoleArn)
LOG_DELIVERY_ROLE = TypeAdapter(LogDeliveryRoleArn)


@pytest.mark.parametrize("partition", ["aws", "aws-us-gov", "aws-cn", "aws-eusc"])
def test_a_source_role_in_any_partition_is_accepted(partition: str) -> None:
    arn = f"arn:{partition}:iam::111122223333:role/CcfSourceRole-github-awslabs"

    assert SOURCE_ROLE.validate_python(arn) == arn


def test_a_log_delivery_role_is_accepted() -> None:
    arn = "arn:aws:iam::111122223333:role/CcfLogDeliveryRole-github-awslabs"

    assert LOG_DELIVERY_ROLE.validate_python(arn) == arn


@pytest.mark.parametrize(
    "arn",
    [
        "arn:aws:iam::111122223333:role/ccf/CcfSourceRole-github",
        "arn:aws:iam::111122223333:role/CcfLogDeliveryRole-github",
        f"arn:aws:iam::111122223333:role/CcfSourceRole-{'a' * 51}",
    ],
    ids=["a path", "another prefix", "a name over 64 characters"],
)
def test_a_source_role_with_a_path_another_prefix_or_a_long_name_is_rejected(arn: str) -> None:
    with pytest.raises(ValidationError):
        SOURCE_ROLE.validate_python(arn)


@pytest.mark.parametrize(
    "arn",
    [
        "arn:aws:iam::111122223333:role/ccf/CcfLogDeliveryRole-github",
        "arn:aws:iam::111122223333:role/CcfSourceRole-github",
        f"arn:aws:iam::111122223333:role/CcfLogDeliveryRole-{'a' * 46}",
    ],
    ids=["a path", "another prefix", "a name over 64 characters"],
)
def test_a_log_delivery_role_with_a_path_another_prefix_or_a_long_name_is_rejected(arn: str) -> None:
    with pytest.raises(ValidationError):
        LOG_DELIVERY_ROLE.validate_python(arn)
