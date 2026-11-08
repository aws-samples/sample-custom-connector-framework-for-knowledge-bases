"""The body of one `CcfIngestionQueue` message, and how long a retried one waits.

Each sender writes a token to the destination's `ingestion_pending` only when none is waiting, and sends
one message with whichever token is waiting, so a failed send leaves the token for the next message. The
token is also Bedrock's `clientToken`, which must be 33 to 256 characters, so it is `ing-` plus a full UUID.

`python -m common.ingestion_requests` prints the message's JSON Schema; the build writes it to
`generated/ingestion-request-message.schema.json`, the contract the CDK's senders are tested against.
"""

from __future__ import annotations

import json
from typing import Annotated

from ccf_api_models.base import ValueModel
from ccf_api_models.models import DestinationId, TenantArnPrefix
from pydantic import ConfigDict, Field, StringConstraints

INGESTION_TOKEN_PATTERN = r"^ing-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"  # noqa: S105 - a pattern


class IngestionRequestMessage(ValueModel):
    """The body of one message on `CcfIngestionQueue`."""

    model_config = ConfigDict(extra="forbid")

    tenant_arn_prefix: TenantArnPrefix
    destination_id: DestinationId
    token: Annotated[str, StringConstraints(pattern=INGESTION_TOKEN_PATTERN)]


class RetryDelays(ValueModel):
    """The visibility delay of a retried message, by how often it was received."""

    seconds: tuple[int, ...] = Field(min_length=1)

    def for_receive(self, receive_count: int) -> int:
        """The delay for this receive; a count below 1 counts as 1, one past the list keeps the last delay."""
        return self.seconds[min(max(receive_count, 1), len(self.seconds)) - 1]


INGESTION_RETRY_DELAYS = RetryDelays(seconds=(60, 120, 240, 480, 900))
"""One, two, four, and eight minutes, then 15 minutes, the longest visibility delay SQS allows."""


if __name__ == "__main__":
    print(json.dumps(IngestionRequestMessage.model_json_schema(), indent=2))
