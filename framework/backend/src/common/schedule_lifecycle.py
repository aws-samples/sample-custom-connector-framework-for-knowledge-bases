"""The EventBridge Scheduler schedule that starts a custom connector's jobs.

Each custom connector has at most one schedule, named `ConnectorRef.schedule_name`. The schedule
invokes the scheduled-start Lambda with a `ScheduledStartEvent`, whose `scheduled_time` and
`correlation_id` EventBridge Scheduler fills in for each invocation: the scheduled time and the
invocation's execution ID, which every log line of that scheduled start carries.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol, Unpack

from botocore.exceptions import ClientError
from ccf_api_models.identifiers import ConnectorRef
from ccf_api_models.models import ConnectorSchedule
from pydantic import BaseModel, ConfigDict

from common.aws_error_code import AwsErrorCode
from common.errors import FailedAwsCall

if TYPE_CHECKING:
    from mypy_boto3_scheduler.type_defs import (
        CreateScheduleInputTypeDef,
        DeleteScheduleInputTypeDef,
        UpdateScheduleInputTypeDef,
    )

    from common.settings import SchedulerSettings

SCHEDULED_TIME_PLACEHOLDER = "<aws.scheduler.scheduled-time>"
"""EventBridge Scheduler's context attribute; each invocation's input carries the scheduled time in its place."""
EXECUTION_ID_PLACEHOLDER = "<aws.scheduler.execution-id>"
"""EventBridge Scheduler's context attribute; each invocation's input carries its execution ID in its place."""
DEFAULT_SCHEDULE_TIMEZONE = "UTC"
"""The time zone EventBridge Scheduler evaluates an expression in when the schedule names none."""


class ScheduledStartEvent(BaseModel):
    """The input every schedule sends to the scheduled-start Lambda."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1]
    """The input's schema version."""
    tenant_arn_prefix: str
    destination_id: str
    connector_id: str
    scheduled_time: str
    correlation_id: str
    """The Scheduler invocation's execution ID."""


class ScheduleLifecycleRequest(BaseModel):
    """A custom connector and the schedule it should have; None means it should have none."""

    model_config = ConfigDict(frozen=True)

    connector: ConnectorRef
    schedule: ConnectorSchedule | None = None


class SchedulerClient(Protocol):
    """The three EventBridge Scheduler calls the lifecycle makes."""

    def create_schedule(self, **arguments: Unpack[CreateScheduleInputTypeDef]) -> object: ...
    def update_schedule(self, **arguments: Unpack[UpdateScheduleInputTypeDef]) -> object: ...
    def delete_schedule(self, **arguments: Unpack[DeleteScheduleInputTypeDef]) -> object: ...


class ScheduleLifecycle:
    """Creates, replaces, and deletes custom connector schedules. A lifecycle manager."""

    def __init__(self, client: SchedulerClient, settings: SchedulerSettings) -> None:
        self._client = client
        self._settings = settings

    def create(self, request: ScheduleLifecycleRequest) -> None:
        """Create the schedule, replacing one a retry already created."""
        arguments = self._arguments(request)
        try:
            self._client.create_schedule(**arguments)
        except ClientError as error:
            if AwsErrorCode.from_client_error(error) is not AwsErrorCode.CONFLICT:
                raise FailedAwsCall(error=error, action="Schedule creation").to_error() from error
            try:
                self._client.update_schedule(**arguments)
            except ClientError as fallback_error:
                raise FailedAwsCall(error=fallback_error, action="Schedule replacement").to_error() from fallback_error

    def replace(self, request: ScheduleLifecycleRequest) -> None:
        """Replace the schedule, creating it when it doesn't exist; no schedule deletes it."""
        if request.schedule is None:
            self.delete(request.connector)
            return
        arguments = self._arguments(request)
        try:
            self._client.update_schedule(**arguments)
        except ClientError as error:
            if AwsErrorCode.from_client_error(error) is not AwsErrorCode.RESOURCE_NOT_FOUND:
                raise FailedAwsCall(error=error, action="Schedule update").to_error() from error
            try:
                self._client.create_schedule(**arguments)
            except ClientError as fallback_error:
                raise FailedAwsCall(error=fallback_error, action="Schedule creation").to_error() from fallback_error

    def delete(self, connector: ConnectorRef) -> None:
        """Delete the schedule; one that doesn't exist is already deleted."""
        try:
            self._client.delete_schedule(Name=connector.schedule_name, GroupName=self._settings.group_name)
        except ClientError as error:
            if AwsErrorCode.from_client_error(error) is not AwsErrorCode.RESOURCE_NOT_FOUND:
                raise FailedAwsCall(error=error, action="Schedule deletion").to_error() from error

    def _arguments(self, request: ScheduleLifecycleRequest) -> CreateScheduleInputTypeDef:
        """What `CreateSchedule` and `UpdateSchedule` both send for the custom connector's schedule."""
        schedule = request.schedule
        if schedule is None:
            raise ValueError("Only a schedule can be created or replaced; delete removes one")
        connector, settings = request.connector, self._settings
        event = ScheduledStartEvent(
            version=1,
            tenant_arn_prefix=connector.tenant.arn_prefix,
            destination_id=connector.destination_id,
            connector_id=connector.connector_id,
            scheduled_time=SCHEDULED_TIME_PLACEHOLDER,
            correlation_id=EXECUTION_ID_PLACEHOLDER,
        )
        return {
            "Name": connector.schedule_name,
            "GroupName": settings.group_name,
            "ScheduleExpression": schedule.expression,
            # EventBridge Scheduler's own default, sent explicitly so both calls take one argument list.
            "ScheduleExpressionTimezone": schedule.timezone or DEFAULT_SCHEDULE_TIMEZONE,
            "State": schedule.state.value,
            "FlexibleTimeWindow": {"Mode": "OFF"},
            "Target": {
                "Arn": settings.target_arn,
                "RoleArn": settings.role_arn,
                "Input": event.model_dump_json(),
                "DeadLetterConfig": {"Arn": settings.dead_letter_arn},
                "RetryPolicy": {
                    "MaximumEventAgeInSeconds": settings.max_event_age_seconds,
                    "MaximumRetryAttempts": settings.retry_attempts,
                },
            },
        }
