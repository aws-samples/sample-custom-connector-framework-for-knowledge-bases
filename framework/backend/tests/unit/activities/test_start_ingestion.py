"""Starting an ingestion from a queue message: duplicates, retries, the dead-letter queue, and metrics."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from ccf_api_models.base import ValueModel

from activities.start_ingestion import IngestionDelivery, IngestionOutcome, StartIngestionActivity
from common.ingestion_requests import INGESTION_RETRY_DELAYS, IngestionRequestMessage
from common.observability import MetricName, MetricRecord
from common.storage.bedrock.bedrock_ingestion_dao import (
    BedrockIngestionDao,
    IngestionDisposition,
    IngestionFailure,
    IngestionStartError,
    StartIngestionRequest,
)
from common.storage.sqs.ingestion_queue_dao import DelayMessageRequest, IngestionQueueDao
from tests.unit.activities.conftest import DATA_SOURCE_ID, KNOWLEDGE_BASE_ID, ActivityWorld
from tests.unit.dao_world import DaoWorld

DESTINATION = DaoWorld.destination
TOKEN = "ing-3f6c2d1e-0000-4000-8000-000000000000"
NEWER_TOKEN = "ing-00000000-0000-4000-8000-000000000000"
QUEUE_URL = "https://sqs.us-east-1.amazonaws.com/444455556666/CcfIngestionQueue"
RETRY = IngestionStartError(
    IngestionFailure(disposition=IngestionDisposition.RETRY, aws_error_code="ThrottlingException")
)
REJECT = IngestionStartError(
    IngestionFailure(disposition=IngestionDisposition.REJECT, aws_error_code="AccessDeniedException")
)


class FakeBedrock(BedrockIngestionDao):
    """Records each start; `error` fails it and `on_start` runs before the error is raised."""

    def __init__(self) -> None:
        self.requests: list[StartIngestionRequest] = []
        self.error: Exception | None = None
        self.on_start: Callable[[], None] = lambda: None

    def start_ingestion(self, request: StartIngestionRequest) -> None:
        self.requests.append(request)
        self.on_start()
        if self.error is not None:
            raise self.error


class FakeQueue(IngestionQueueDao):
    """Records each delay and each dead-lettered body."""

    def __init__(self) -> None:
        self.delays: list[int] = []
        self.dead_lettered: list[str] = []

    def delay(self, request: DelayMessageRequest) -> None:
        assert request.queue_url == QUEUE_URL
        self.delays.append(request.delay_seconds)

    def dead_letter(self, body: str) -> None:
        self.dead_lettered.append(body)


class Delivery(ValueModel):
    body: str
    receive_count: int = 1


class IngestionWorld:
    """The activity over the activity world's destination, which waits with `TOKEN`."""

    def __init__(self, world: ActivityWorld) -> None:
        self.world = world
        self.bedrock = FakeBedrock()
        self.queue = FakeQueue()
        self.activity = StartIngestionActivity(
            world.daos.destinations, self.bedrock, self.queue, world.metrics, INGESTION_RETRY_DELAYS
        )
        self.set_pending(TOKEN)

    def update(self, expression: str, values: dict[str, Any]) -> None:
        self.world.daos.table(self.world.daos.names.destinations).update_item(
            Key={"tenant_arn_prefix": DESTINATION.tenant.arn_prefix, "destination_id": DESTINATION.destination_id},
            UpdateExpression=expression,
            **({"ExpressionAttributeNames": {"#status": "status"}} if "#status" in expression else {}),
            ExpressionAttributeValues=values,
        )

    def set_pending(self, token: str) -> None:
        self.update("SET ingestion_pending = :token", {":token": token})

    def deliver(self, receive_count: int = 1) -> IngestionOutcome:
        message = IngestionRequestMessage(
            tenant_arn_prefix=DESTINATION.tenant.arn_prefix, destination_id=DESTINATION.destination_id, token=TOKEN
        )
        return self.deliver_body(Delivery(body=message.model_dump_json(), receive_count=receive_count))

    def deliver_body(self, delivery: Delivery) -> IngestionOutcome:
        return self.activity.start(
            IngestionDelivery(
                body=delivery.body,
                message_id="message-1",
                queue_url=QUEUE_URL,
                receipt_handle="receipt-1",
                receive_count=delivery.receive_count,
            )
        )

    def pending(self) -> str | None:
        return self.world.daos.destinations.get(DESTINATION).ingestion_pending

    def metric_names(self) -> list[MetricName]:
        return [record.name for record in self.world.metrics.records]


@pytest.fixture
def world(activity_world: ActivityWorld) -> IngestionWorld:
    return IngestionWorld(activity_world)


def test_a_message_starts_an_ingestion_with_its_token_as_client_token_and_clears_it(world: IngestionWorld) -> None:
    assert world.deliver() is IngestionOutcome.STARTED

    [request] = world.bedrock.requests
    assert (request.client_token, request.knowledge_base_id, request.data_source_id) == (
        TOKEN,
        KNOWLEDGE_BASE_ID,
        DATA_SOURCE_ID,
    )
    assert world.pending() is None
    assert world.metric_names() == [MetricName.INGESTION_JOBS_STARTED]


def test_a_destination_without_a_knowledge_base_drops_the_message_without_a_call(world: IngestionWorld) -> None:
    world.update("SET destination_resources = :resources", {":resources": {}})

    assert world.deliver() is IngestionOutcome.SKIPPED
    assert world.bedrock.requests == []
    assert world.queue.dead_lettered == []


def test_a_duplicate_token_is_dropped_without_a_call(world: IngestionWorld) -> None:
    world.deliver()

    assert world.deliver() is IngestionOutcome.SKIPPED
    assert len(world.bedrock.requests) == 1


def test_a_deleting_destination_drops_the_message_without_a_call(world: IngestionWorld) -> None:
    world.update("SET #status = :status", {":status": "DELETING"})

    assert world.deliver() is IngestionOutcome.SKIPPED
    assert world.bedrock.requests == []


def test_a_deleted_destination_drops_the_message_without_a_call(world: IngestionWorld) -> None:
    world.world.daos.table(world.world.daos.names.destinations).delete_item(
        Key={"tenant_arn_prefix": DESTINATION.tenant.arn_prefix, "destination_id": DESTINATION.destination_id}
    )

    assert world.deliver() is IngestionOutcome.SKIPPED
    assert world.bedrock.requests == []
    assert world.queue.dead_lettered == []


def test_a_retry_restores_the_token_and_delays_the_message(world: IngestionWorld) -> None:
    world.bedrock.error = RETRY

    assert world.deliver(receive_count=1) is IngestionOutcome.RETRY
    assert world.queue.delays == [60]
    assert world.pending() == TOKEN
    assert world.queue.dead_lettered == []


def test_retry_delays_grow_with_the_receive_count(world: IngestionWorld) -> None:
    world.bedrock.error = RETRY

    for receive_count in range(1, 7):
        world.deliver(receive_count)

    assert world.queue.delays == [60, 120, 240, 480, 900, 900]


def test_a_retry_keeps_a_newer_waiting_token(world: IngestionWorld) -> None:
    world.bedrock.error = RETRY
    world.bedrock.on_start = lambda: world.set_pending(NEWER_TOKEN)

    world.deliver()

    assert world.pending() == NEWER_TOKEN


def test_a_rejected_start_dead_letters_the_message_and_records_a_failed_job(world: IngestionWorld) -> None:
    world.bedrock.error = REJECT

    assert world.deliver() is IngestionOutcome.DEAD_LETTERED

    assert len(world.queue.dead_lettered) == 1
    assert world.queue.delays == []
    assert world.metric_names() == [MetricName.INGESTION_JOBS_FAILED]


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("not json", id="unparseable"),
        pytest.param(
            '{"tenant_arn_prefix":"arn:aws:ccf:us-east-1:111122223333","destination_id":"dst-7c41e9a2b5d0"}',
            id="missing-token",
        ),
        pytest.param(
            f'{{"tenant_arn_prefix":"arn:aws:ccf:nowhere","destination_id":"dst-7c41e9a2b5d0","token":"{TOKEN}"}}',
            id="unknown-tenant-prefix",
        ),
    ],
)
def test_a_body_that_isnt_a_valid_message_is_dead_lettered_once_and_counted(world: IngestionWorld, body: str) -> None:
    assert world.deliver_body(Delivery(body=body)) is IngestionOutcome.DEAD_LETTERED

    assert world.queue.dead_lettered == [body]
    assert world.bedrock.requests == []
    assert world.world.metrics.records == [MetricRecord(name=MetricName.INGESTION_JOBS_FAILED, value=1)]
    assert world.pending() == TOKEN
