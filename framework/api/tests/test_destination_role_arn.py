"""CreateDestination accepts only a role at role/ccf/destinations/CcfDestinationRole-<name>."""

import json
import re
from pathlib import Path

import pytest

OPENAPI = Path(__file__).resolve().parents[1] / "generated" / "openapi.json"
SUFFIX = "a" * 45


def _schema(name: str) -> dict[str, object]:
    schemas: dict[str, dict[str, object]] = json.loads(OPENAPI.read_text())["components"]["schemas"]
    return schemas[name]


def _role_arn_pattern(request_schema: str) -> re.Pattern[str]:
    """The pattern of the schema the request's `role_arn` member refers to."""
    member = _schema(request_schema)["properties"]
    assert isinstance(member, dict)
    reference = member["role_arn"]["$ref"]
    pattern = _schema(reference.rsplit("/", 1)[1])["pattern"]
    assert isinstance(pattern, str)
    return re.compile(pattern)


CREATE = _role_arn_pattern("CreateDestinationRequestContent")


@pytest.mark.parametrize("partition", ["aws", "aws-us-gov", "aws-cn", "aws-eusc"])
def test_a_45_character_suffix_is_accepted_in_each_partition(partition: str) -> None:
    arn = f"arn:{partition}:iam::111122223333:role/ccf/destinations/CcfDestinationRole-{SUFFIX}"

    assert CREATE.fullmatch(arn)


def test_the_role_outside_the_destinations_path_is_rejected() -> None:
    assert not CREATE.fullmatch("arn:aws:iam::111122223333:role/ccf/CcfDestinationRole")


def test_a_destination_role_name_outside_ccf_destinations_is_rejected() -> None:
    assert not CREATE.fullmatch(f"arn:aws:iam::111122223333:role/ccf/CcfDestinationRole-{SUFFIX}")


def test_a_46_character_suffix_is_rejected() -> None:
    arn = f"arn:aws:iam::111122223333:role/ccf/destinations/CcfDestinationRole-{SUFFIX}a"

    assert not CREATE.fullmatch(arn)


def test_a_suffix_with_a_slash_is_rejected() -> None:
    assert not CREATE.fullmatch("arn:aws:iam::111122223333:role/ccf/destinations/CcfDestinationRole-docs/extra")


def test_a_destination_still_returns_any_role_arn() -> None:
    returned = _role_arn_pattern("Destination")

    assert returned.fullmatch("arn:aws:iam::111122223333:role/ccf/CcfDestinationRole")
