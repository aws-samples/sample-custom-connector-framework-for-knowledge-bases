"""Service-owned EventBridge Scheduler lifecycle tests."""

from unittest.mock import MagicMock

from botocore.exceptions import ClientError
from ccf_api_models.models import ConnectorSchedule, ScheduleState

from common.schedule_lifecycle import ScheduleIdentity, ScheduleLifecycle, ScheduleLifecycleRequest
from common.tenant import TenantContext

_TENANT = TenantContext("123456789012", "us-east-1")
_IDENTITY = ScheduleIdentity(_TENANT, "connector-1")
_SCHEDULE = ConnectorSchedule(
    expression="rate(15 minutes)",
    state=ScheduleState.ENABLED,
    timezone="America/Los_Angeles",
)


def client_error(code: str, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "test"}}, operation)


def create_lifecycle(client: MagicMock) -> ScheduleLifecycle:
    return ScheduleLifecycle(
        scheduler_client=client,
        target_arn="arn:aws:lambda:us-east-1:123456789012:function:scheduled-start",
        role_arn="arn:aws:iam::123456789012:role/scheduler-target",
        group_name="ccf",
        dead_letter_arn="arn:aws:sqs:us-east-1:123456789012:scheduler-dlq",
    )


def test_create_and_update_use_complete_schedule_arguments() -> None:
    client = MagicMock()
    lifecycle = create_lifecycle(client)
    request = ScheduleLifecycleRequest(_IDENTITY, _SCHEDULE)

    lifecycle.create(request)
    lifecycle.create(request)
    lifecycle.update(request)

    first = client.create_schedule.call_args_list[0].kwargs
    second = client.create_schedule.call_args_list[1].kwargs
    updated = client.update_schedule.call_args.kwargs
    assert first == second == updated
    assert "ClientToken" not in first
    assert first["Name"].startswith("ccf-")
    assert len(first["Name"]) == 36
    assert first["GroupName"] == "ccf"
    assert first["ScheduleExpression"] == "rate(15 minutes)"
    assert first["ScheduleExpressionTimezone"] == "America/Los_Angeles"
    assert first["State"] == "ENABLED"
    assert first["FlexibleTimeWindow"] == {"Mode": "OFF"}
    assert first["Target"]["Arn"].endswith(":function:scheduled-start")
    assert first["Target"]["RoleArn"].endswith(":role/scheduler-target")
    assert first["Target"]["DeadLetterConfig"] == {"Arn": "arn:aws:sqs:us-east-1:123456789012:scheduler-dlq"}
    assert first["Target"]["RetryPolicy"] == {
        "MaximumEventAgeInSeconds": 3600,
        "MaximumRetryAttempts": 3,
    }
    assert first["Target"]["Input"] == (
        '{"connector_id":"connector-1","custom_connector_arn_prefix":'
        '"arn:aws:ccf:us-east-1:123456789012","scheduled_time":'
        '"<aws.scheduler.scheduled-time>"}'
    )


def test_create_conflict_reconciles_with_update() -> None:
    client = MagicMock()
    client.create_schedule.side_effect = client_error("ConflictException", "CreateSchedule")
    lifecycle = create_lifecycle(client)

    lifecycle.create(ScheduleLifecycleRequest(_IDENTITY, _SCHEDULE))

    client.update_schedule.assert_called_once_with(**client.create_schedule.call_args.kwargs)


def test_update_missing_schedule_reconciles_with_create() -> None:
    client = MagicMock()
    client.update_schedule.side_effect = client_error("ResourceNotFoundException", "UpdateSchedule")
    lifecycle = create_lifecycle(client)

    lifecycle.update(ScheduleLifecycleRequest(_IDENTITY, _SCHEDULE))

    client.create_schedule.assert_called_once_with(**client.update_schedule.call_args.kwargs)


def test_delete_removes_deterministic_schedule() -> None:
    client = MagicMock()
    lifecycle = create_lifecycle(client)

    lifecycle.delete(_IDENTITY)

    client.delete_schedule.assert_called_once_with(
        Name="ccf-76757980f1a569b7fd4e16b65e986be2",
        GroupName="ccf",
    )


def test_delete_missing_schedule_is_idempotent() -> None:
    client = MagicMock()
    client.delete_schedule.side_effect = client_error("ResourceNotFoundException", "DeleteSchedule")
    lifecycle = create_lifecycle(client)

    lifecycle.delete(_IDENTITY)

    client.delete_schedule.assert_called_once_with(
        Name="ccf-76757980f1a569b7fd4e16b65e986be2",
        GroupName="ccf",
    )
