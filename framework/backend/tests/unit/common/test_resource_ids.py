"""New resource IDs match their generated ID patterns."""

import re
from decimal import Decimal
from typing import Any

import pytest
from ccf_api_models.identifiers import GeneratedIdKind
from ccf_api_models.models import ConnectorId, DestinationId, JobId
from pydantic import TypeAdapter

from common.resource_ids import ResourceIds, SystemClock

# Each alias is a `type` statement, which TypeAdapter accepts but its stub doesn't name.
ALIASES: dict[GeneratedIdKind, Any] = {
    GeneratedIdKind.DESTINATION: DestinationId,
    GeneratedIdKind.CUSTOM_CONNECTOR: ConnectorId,
    GeneratedIdKind.JOB: JobId,
}


@pytest.mark.parametrize("kind", list(GeneratedIdKind))
def test_a_new_id_of_each_kind_matches_its_generated_alias(kind: GeneratedIdKind) -> None:
    TypeAdapter(ALIASES[kind]).validate_python(ResourceIds().new(kind))


def test_a_new_id_is_its_prefix_and_12_hex_characters() -> None:
    assert re.fullmatch(r"dst-[0-9a-f]{12}", ResourceIds().new(GeneratedIdKind.DESTINATION))


def test_two_new_ids_differ() -> None:
    ids = ResourceIds()

    assert ids.new(GeneratedIdKind.JOB) != ids.new(GeneratedIdKind.JOB)


def test_the_clock_returns_epoch_seconds_as_a_decimal() -> None:
    assert isinstance(SystemClock().now(), Decimal)
