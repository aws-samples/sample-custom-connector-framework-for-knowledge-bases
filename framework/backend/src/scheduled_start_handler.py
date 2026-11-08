"""EventBridge Scheduler target that starts a custom connector's scheduled job."""

from __future__ import annotations

from typing import Any

import boto3
from aws_lambda_powertools.utilities.typing import LambdaContext
from ccf_api_models.catalog import OperationCatalog
from ccf_api_models.operations import Operation

from activities.jobs import StartCustomConnectorJobActivity
from activities.scheduled_start import ScheduledClientToken, ScheduledStart, ScheduledStartActivity
from common.composition import JobsDaoRequest, build_jobs_dao
from common.idempotency import IdempotentCreates
from common.observability import logger, tracer
from common.resource_data_codec import KmsResourceDataCodec
from common.resource_ids import ResourceIds
from common.schedule_lifecycle import ScheduledStartEvent
from common.settings import ScheduledStartSettings
from common.workflow_executions import WorkflowExecutions

settings = ScheduledStartSettings()
idempotency = IdempotentCreates.from_settings(settings)
scheduled_starts = ScheduledStartActivity(
    StartCustomConnectorJobActivity(
        build_jobs_dao(JobsDaoRequest(tables=settings.tables, jobs=settings.jobs)),
        KmsResourceDataCodec.for_key(settings.resource_data_key_arn),
        WorkflowExecutions(boto3.client("stepfunctions"), settings.jobs.job_execution_arn),
        ResourceIds(),
    ),
    idempotency,
    OperationCatalog.load().spec(Operation.START_CUSTOM_CONNECTOR_JOB),
    ScheduledClientToken(),
)


@logger.inject_lambda_context(clear_state=True)
@tracer.capture_lambda_handler
def handler(event: dict[str, Any], context: LambdaContext) -> None:
    """Start one scheduled job, at most once per scheduled time."""
    idempotency.register_lambda_context(context)
    scheduled = ScheduledStartEvent.model_validate(event)
    logger.append_keys(correlation_id=scheduled.correlation_id)
    scheduled_starts.start(ScheduledStart(event=scheduled, invocation_id=context.aws_request_id))
