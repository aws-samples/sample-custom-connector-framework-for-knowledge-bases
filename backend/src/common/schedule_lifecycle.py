"""Service-owned EventBridge Scheduler lifecycle for connector schedules."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from botocore.exceptions import ClientError
from ccf_api_models.models import ConnectorSchedule

from common.tenant import TenantContext

if TYPE_CHECKING:
    from mypy_boto3_scheduler import EventBridgeSchedulerClient

_SCHEDULER_RETRY_ATTEMPTS = 3
_SCHEDULER_MAX_EVENT_AGE_SECONDS = 3600


@dataclass(frozen=True)
class ScheduleIdentity:
    """Stable connector identity used for a deterministic schedule."""

    tenant_context: TenantContext
    connector_id: str


@dataclass(frozen=True)
class ScheduleLifecycleRequest:
    """Desired schedule and connector identity."""

    identity: ScheduleIdentity
    schedule: ConnectorSchedule


class ScheduleLifecycle:
    """Creates, replaces, and deletes service-owned connector schedules."""

    def __init__(
        self,
        scheduler_client: EventBridgeSchedulerClient,
        target_arn: str,
        role_arn: str,
        group_name: str,
        dead_letter_arn: str,
    ) -> None:
        self._scheduler_client = scheduler_client
        self._target_arn = target_arn
        self._role_arn = role_arn
        self._group_name = group_name
        self._dead_letter_arn = dead_letter_arn

    def create(self, request: ScheduleLifecycleRequest) -> None:
        """Create the desired schedule, replacing an existing schedule on retry."""
        try:
            self._scheduler_client.create_schedule(**self._schedule_arguments(request))
        except ClientError as error:
            if self._error_code(error) != "ConflictException":
                raise
            self._scheduler_client.update_schedule(**self._schedule_arguments(request))

    def update(self, request: ScheduleLifecycleRequest) -> None:
        """Replace the desired schedule, creating it when it is missing."""
        try:
            self._scheduler_client.update_schedule(**self._schedule_arguments(request))
        except ClientError as error:
            if self._error_code(error) != "ResourceNotFoundException":
                raise
            self._scheduler_client.create_schedule(**self._schedule_arguments(request))

    def delete(self, identity: ScheduleIdentity) -> None:
        """Delete a schedule and treat an already-missing schedule as success."""
        try:
            self._scheduler_client.delete_schedule(
                Name=self._schedule_name(identity),
                GroupName=self._group_name,
            )
        except ClientError as error:
            if self._error_code(error) != "ResourceNotFoundException":
                raise

    def _schedule_arguments(self, request: ScheduleLifecycleRequest) -> dict[str, Any]:
        schedule = request.schedule
        arguments: dict[str, Any] = {
            "Name": self._schedule_name(request.identity),
            "GroupName": self._group_name,
            "ScheduleExpression": schedule.expression,
            "State": schedule.state.value,
            "FlexibleTimeWindow": {"Mode": "OFF"},
            "Target": {
                "Arn": self._target_arn,
                "RoleArn": self._role_arn,
                "Input": self._target_input(request.identity),
                "DeadLetterConfig": {"Arn": self._dead_letter_arn},
                "RetryPolicy": {
                    "MaximumEventAgeInSeconds": _SCHEDULER_MAX_EVENT_AGE_SECONDS,
                    "MaximumRetryAttempts": _SCHEDULER_RETRY_ATTEMPTS,
                },
            },
        }
        if schedule.timezone is not None:
            arguments["ScheduleExpressionTimezone"] = schedule.timezone
        return arguments

    @staticmethod
    def _schedule_name(identity: ScheduleIdentity) -> str:
        resource_identity = f"{identity.tenant_context.get_arn_prefix()}:{identity.connector_id}"
        identity_hash = hashlib.sha256(resource_identity.encode("utf-8")).hexdigest()[:32]
        return f"ccf-{identity_hash}"

    @staticmethod
    def _target_input(identity: ScheduleIdentity) -> str:
        return json.dumps(
            {
                "custom_connector_arn_prefix": identity.tenant_context.get_arn_prefix(),
                "connector_id": identity.connector_id,
                "scheduled_time": "<aws.scheduler.scheduled-time>",
            },
            separators=(",", ":"),
            sort_keys=True,
        )

    @staticmethod
    def _error_code(error: ClientError) -> str:
        return str(error.response.get("Error", {}).get("Code", ""))
