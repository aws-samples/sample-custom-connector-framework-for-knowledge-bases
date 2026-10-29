"""Shared Lambda target for every service-owned connector schedule."""

from datetime import UTC, datetime
from typing import Any

from aws_lambda_powertools.utilities.typing import LambdaContext
from ccf_api_models.base import CcfModel
from ccf_api_models.models import StartCustomConnectorJobRequest
from pydantic import ConfigDict, Field, SecretStr, field_validator

from api_handler import start_job_activity
from common.observability import logger, tracer
from common.operation import OperationRequest
from common.tenant import TenantContext


class ScheduledStartEvent(CcfModel):
    """Validated identity supplied by the service-owned schedule target."""

    model_config = ConfigDict(extra="forbid")

    custom_connector_arn_prefix: str = Field(min_length=1, max_length=2048)
    connector_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$",
    )
    scheduled_time: datetime

    @field_validator("scheduled_time", mode="before")
    @classmethod
    def validate_scheduled_time(cls, value: Any) -> datetime:
        """Require an aware ISO-8601 string and normalize it to UTC."""
        if not isinstance(value, str):
            raise ValueError("scheduled_time must be an ISO-8601 string")
        try:
            scheduled_time = datetime.fromisoformat(value)
        except ValueError as error:
            raise ValueError("scheduled_time must be an ISO-8601 timestamp") from error
        if scheduled_time.tzinfo is None or scheduled_time.utcoffset() is None:
            raise ValueError("scheduled_time must include a UTC offset")
        return scheduled_time.astimezone(UTC)


@logger.inject_lambda_context(clear_state=True)
@tracer.capture_lambda_handler
def handler(event: dict[str, Any], _context: LambdaContext) -> dict[str, Any]:
    """Start one scheduled job through the existing start activity."""
    scheduled_event = ScheduledStartEvent.model_validate(event)
    tenant_context = TenantContext.from_arn_prefix(scheduled_event.custom_connector_arn_prefix)
    response = start_job_activity.start(
        OperationRequest(
            tenant=tenant_context,
            payload=StartCustomConnectorJobRequest(
                connector_id=scheduled_event.connector_id,
                job_configuration=SecretStr("{}"),
            ),
            idempotency_key=scheduled_event.scheduled_time.isoformat(timespec="microseconds"),
        )
    )
    logger.info(
        "scheduled_connector_job_started",
        extra={
            "account_id": tenant_context.account_id,
            "connector_id": scheduled_event.connector_id,
            "job_id": response.job.job_id,
        },
    )
    return response.model_dump(mode="json")
