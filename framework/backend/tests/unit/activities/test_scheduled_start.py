"""The scheduled start over the moto tables: one job per scheduled time, and a skip while a job runs."""

from __future__ import annotations

from typing import Any

import pytest
from ccf_api_models.base import ValueModel
from ccf_api_models.catalog import OperationCatalog
from ccf_api_models.models import ConnectorStatus
from ccf_api_models.operations import Operation

from activities.jobs import StartCustomConnectorJobActivity
from activities.scheduled_start import ScheduledClientToken, ScheduledStart, ScheduledStartActivity
from common.idempotency import IdempotentCreates
from common.resource_data_codec import ResourceDataCodec
from common.resource_ids import ResourceIds
from common.schedule_lifecycle import ScheduledStartEvent
from common.settings import IdempotencySettings, TableNames
from common.workflow_executions import WorkflowExecutions
from tests.unit.activities.conftest import ActivitySeed, ActivityWorld
from tests.unit.activities.test_jobs import FakeStepFunctions
from tests.unit.dao_world import JOB_SETTINGS

EVENT = ScheduledStartEvent(
    version=1,
    tenant_arn_prefix="arn:aws:ccf:us-east-1:111122223333",
    destination_id="dst-7c41e9a2b5d0",
    connector_id="cc-e020d65d76c0",
    scheduled_time="2026-11-01T02:00:00Z",
    correlation_id="4f3e2d1c-execution",
)
KNOWN_TOKEN = "scheduled-423be79867ec24295bea8a823c91252868e3cf6964a7ffe7"


class _IdempotencyRoot(ValueModel):
    tables: TableNames
    idempotency: IdempotencySettings


class ScheduledWorld:
    """The scheduled start over the activity world's tables, an idempotency table, and a recording Step Functions."""

    def __init__(self, world: ActivityWorld, codec: ResourceDataCodec) -> None:
        names = world.daos.names
        world.daos.resource.create_table(
            TableName=names.idempotency,
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
        )
        self.sfn = FakeStepFunctions()
        executions = WorkflowExecutions(self.sfn, JOB_SETTINGS.job_execution_arn)  # type: ignore[arg-type]
        self.activity = ScheduledStartActivity(
            StartCustomConnectorJobActivity(world.daos.jobs, codec, executions, ResourceIds()),
            IdempotentCreates.from_settings(
                _IdempotencyRoot(tables=names, idempotency=IdempotencySettings(expiry_seconds=3600))
            ),
            OperationCatalog.load().spec(Operation.START_CUSTOM_CONNECTOR_JOB),
            ScheduledClientToken(),
        )

    def deliver(self, invocation_id: str = "lambda-request-1") -> Any:
        return self.activity.start(ScheduledStart(event=EVENT, invocation_id=invocation_id))


@pytest.fixture
def scheduled(activity_world: ActivityWorld, codec: ResourceDataCodec) -> ScheduledWorld:
    return ScheduledWorld(activity_world, codec)


def test_the_client_token_of_a_known_event_is_unchanged() -> None:
    assert ScheduledClientToken().for_event(EVENT) == KNOWN_TOKEN


def test_another_scheduled_time_gets_another_token() -> None:
    later = EVENT.model_copy(update={"scheduled_time": "2026-11-08T02:00:00Z"})

    assert ScheduledClientToken().for_event(later) != KNOWN_TOKEN


def test_a_redelivered_event_returns_the_first_job_and_starts_one_execution(scheduled: ScheduledWorld) -> None:
    first = scheduled.deliver()
    second = scheduled.deliver("lambda-request-2")

    assert second.job.job_id == first.job.job_id
    assert len(scheduled.sfn.started) == 1


@pytest.mark.parametrize("activity_seed", [ActivitySeed(connector_status=ConnectorStatus.IN_USE)])
def test_a_connector_in_use_skips_the_scheduled_time_with_a_warning(
    scheduled: ScheduledWorld, caplog: pytest.LogCaptureFixture
) -> None:
    assert scheduled.deliver() is None

    [skipped] = [record for record in caplog.records if record.getMessage() == "scheduled_job_skipped"]
    assert (skipped.levelname, skipped.__dict__["connector_id"]) == ("WARNING", EVENT.connector_id)
    assert scheduled.sfn.started == []
