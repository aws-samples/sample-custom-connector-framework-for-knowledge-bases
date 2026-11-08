"""Custom connector schedules: their target input, and retries that find one already there or none."""

from __future__ import annotations

import json
from typing import Any

import pytest
from botocore.exceptions import ClientError
from ccf_api_models.identifiers import ConnectorRef, DestinationRef, Partition, TenantContext
from ccf_api_models.models import ConnectorSchedule, ScheduleState
from pydantic import ValidationError

from common.schedule_lifecycle import (
    SCHEDULED_TIME_PLACEHOLDER,
    ScheduledStartEvent,
    ScheduleLifecycle,
    ScheduleLifecycleRequest,
)
from common.settings import SchedulerSettings

TENANT = TenantContext(partition=Partition.AWS, region="us-east-1", account_id="111122223333")
CONNECTOR = ConnectorRef(
    destination=DestinationRef(tenant=TENANT, destination_id="dst-7c41e9a2b5d0"), connector_id="cc-e020d65d76c0"
)
SCHEDULE = ConnectorSchedule(expression="cron(0 2 ? * SUN *)", state=ScheduleState.ENABLED, timezone="UTC")
REQUEST = ScheduleLifecycleRequest(connector=CONNECTOR, schedule=SCHEDULE)
SETTINGS = SchedulerSettings(
    target_arn="arn:aws:lambda:us-east-1:444455556666:function:CcfScheduledStart",
    role_arn="arn:aws:iam::444455556666:role/ccf/CcfSchedulerRole",
    group_name="ccf-connectors",
    dead_letter_arn="arn:aws:sqs:us-east-1:444455556666:CcfSchedulerDlq",
    retry_attempts=7,
    max_event_age_seconds=1800,
)


class FakeScheduler:
    """Holds schedules by name, as EventBridge Scheduler does, and records each call."""

    def __init__(self) -> None:
        self.schedules: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []

    def create_schedule(self, **arguments: Any) -> object:
        self.calls.append("CreateSchedule")
        if arguments["Name"] in self.schedules:
            raise ClientError({"Error": {"Code": "ConflictException", "Message": "exists"}}, "CreateSchedule")
        self.schedules[arguments["Name"]] = arguments
        return {}

    def update_schedule(self, **arguments: Any) -> object:
        self.calls.append("UpdateSchedule")
        if arguments["Name"] not in self.schedules:
            raise ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "none"}}, "UpdateSchedule")
        self.schedules[arguments["Name"]] = arguments
        return {}

    def delete_schedule(self, **arguments: Any) -> object:
        self.calls.append("DeleteSchedule")
        self.schedules.pop(arguments["Name"], None)
        return {}


def test_the_target_retries_with_the_settings_attempts_and_event_age() -> None:
    scheduler = FakeScheduler()

    ScheduleLifecycle(scheduler, SETTINGS).create(REQUEST)

    target = scheduler.schedules[CONNECTOR.schedule_name]["Target"]
    assert target["RetryPolicy"] == {"MaximumEventAgeInSeconds": 1800, "MaximumRetryAttempts": 7}


def test_the_target_input_is_the_scheduled_start_event() -> None:
    scheduler = FakeScheduler()

    ScheduleLifecycle(scheduler, SETTINGS).create(REQUEST)

    sent = scheduler.schedules[CONNECTOR.schedule_name]
    assert json.loads(sent["Target"]["Input"]) == {
        "version": 1,
        "tenant_arn_prefix": "arn:aws:ccf:us-east-1:111122223333",
        "destination_id": "dst-7c41e9a2b5d0",
        "connector_id": "cc-e020d65d76c0",
        "scheduled_time": "<aws.scheduler.scheduled-time>",
        "correlation_id": "<aws.scheduler.execution-id>",
    }
    assert sent["ScheduleExpressionTimezone"] == "UTC"


def test_an_event_without_a_version_is_rejected() -> None:
    with pytest.raises(ValidationError, match="version"):
        ScheduledStartEvent.model_validate(
            {
                "tenant_arn_prefix": "arn:aws:ccf:us-east-1:111122223333",
                "destination_id": "dst-7c41e9a2b5d0",
                "connector_id": "cc-e020d65d76c0",
                "scheduled_time": SCHEDULED_TIME_PLACEHOLDER,
                "correlation_id": "4f3e2d1c-execution",
            }
        )


def test_creating_a_schedule_that_exists_replaces_it() -> None:
    scheduler = FakeScheduler()
    lifecycle = ScheduleLifecycle(scheduler, SETTINGS)
    lifecycle.create(
        ScheduleLifecycleRequest(connector=CONNECTOR, schedule=SCHEDULE.model_copy(update={"timezone": None}))
    )

    lifecycle.create(REQUEST)

    assert scheduler.calls == ["CreateSchedule", "CreateSchedule", "UpdateSchedule"]
    assert scheduler.schedules[CONNECTOR.schedule_name]["ScheduleExpression"] == SCHEDULE.expression


def test_replacing_a_schedule_that_doesnt_exist_creates_it() -> None:
    scheduler = FakeScheduler()

    ScheduleLifecycle(scheduler, SETTINGS).replace(REQUEST)

    assert scheduler.calls == ["UpdateSchedule", "CreateSchedule"]
    assert scheduler.schedules[CONNECTOR.schedule_name]["State"] == "ENABLED"


def test_replacing_with_no_schedule_deletes_it() -> None:
    scheduler = FakeScheduler()
    lifecycle = ScheduleLifecycle(scheduler, SETTINGS)
    lifecycle.create(REQUEST)

    lifecycle.replace(ScheduleLifecycleRequest(connector=CONNECTOR))

    assert scheduler.schedules == {}


def test_create_given_a_schedule_without_a_timezone_when_sent_then_names_utc() -> None:
    scheduler = FakeScheduler()
    schedule = ConnectorSchedule(expression="cron(0 2 ? * SUN *)", state=ScheduleState.ENABLED)

    ScheduleLifecycle(scheduler, SETTINGS).create(ScheduleLifecycleRequest(connector=CONNECTOR, schedule=schedule))

    assert scheduler.schedules[CONNECTOR.schedule_name]["ScheduleExpressionTimezone"] == "UTC"
