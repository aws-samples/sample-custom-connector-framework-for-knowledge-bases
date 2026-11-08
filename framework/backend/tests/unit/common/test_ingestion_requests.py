"""The ingestion request message and the retry delays."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from common.ingestion_requests import IngestionRequestMessage, RetryDelays

MESSAGE = {
    "tenant_arn_prefix": "arn:aws:ccf:us-east-1:111122223333",
    "destination_id": "dst-7c41e9a2b5d0",
    "token": "ing-0f1e2d3c-4b5a-4978-8796-a5b4c3d2e1f0",  # gitleaks:allow (an ingestion request ID, not a secret)
}


def test_a_well_formed_message_parses() -> None:
    message = IngestionRequestMessage.model_validate_json(json.dumps(MESSAGE))

    assert message.model_dump() == MESSAGE


@pytest.mark.parametrize(
    ("member", "value"),
    [
        ("tenant_arn_prefix", "arn:aws:ccf:nowhere"),
        ("destination_id", "dst-123"),
        ("token", "ing-not-a-uuid"),
    ],
)
def test_a_malformed_member_fails_validation(member: str, value: str) -> None:
    with pytest.raises(ValidationError, match=member):
        IngestionRequestMessage.model_validate_json(json.dumps({**MESSAGE, member: value}))


def test_an_unknown_member_fails_validation() -> None:
    body: dict[str, Any] = {**MESSAGE, "connector_id": "cc-e020d65d76c0"}

    with pytest.raises(ValidationError, match="connector_id"):
        IngestionRequestMessage.model_validate_json(json.dumps(body))


def test_the_delay_grows_with_the_receive_count_and_keeps_the_last_one() -> None:
    delays = RetryDelays(seconds=(60, 120, 240, 480, 900))

    assert [delays.for_receive(count) for count in range(1, 7)] == [60, 120, 240, 480, 900, 900]


def test_a_receive_count_below_one_counts_as_the_first() -> None:
    assert RetryDelays(seconds=(60, 120)).for_receive(0) == 60


def test_an_empty_delay_list_fails_validation() -> None:
    with pytest.raises(ValidationError, match="seconds"):
        RetryDelays(seconds=())
