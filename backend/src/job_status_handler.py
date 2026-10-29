"""Lambda handler for reconciling terminal AWS Batch job events."""

from typing import Any

import boto3
from aws_lambda_powertools.utilities.typing import LambdaContext
from ccf_api_models.models import DestinationMode

from activities.job_completion import CompleteBatchJobActivity
from common.batch_job_definition import BatchJobDefinitionCleanup
from common.env import (
    BEDROCK_DATA_SOURCE_ID,
    BEDROCK_KNOWLEDGE_BASE_ID,
    CUSTOM_CONNECTOR_JOBS_TABLE_NAME,
    CUSTOM_CONNECTORS_TABLE_NAME,
    DESTINATION_MODE,
)
from common.observability import LogContext, create_log_context, logger, tracer
from common.storage.ddb.custom_connector_jobs_dao import (
    BatchTerminalStatus,
    CustomConnectorJobsDao,
    ReconcileBatchJobStatusRequest,
)
from common.storage.ddb.custom_connectors_dao import CustomConnectorsDao
from common.tenant import TenantContext

dynamodb = boto3.resource("dynamodb")
connectors_dao = CustomConnectorsDao(dynamodb.Table(CUSTOM_CONNECTORS_TABLE_NAME))
jobs_dao = CustomConnectorJobsDao(
    dynamodb.Table(CUSTOM_CONNECTOR_JOBS_TABLE_NAME),
    connectors_dao,
    dynamodb.meta.client,
)
batch_job_definition_cleanup = BatchJobDefinitionCleanup(
    batch_client=boto3.client("batch"),
    jobs_dao=jobs_dao,
)
complete_job_activity = CompleteBatchJobActivity(
    jobs_dao=jobs_dao,
    job_definition_cleanup=batch_job_definition_cleanup,
    bedrock_agent_client=boto3.client("bedrock-agent"),
    destination_mode=DestinationMode(DESTINATION_MODE),
    knowledge_base_id=BEDROCK_KNOWLEDGE_BASE_ID,
    data_source_id=BEDROCK_DATA_SOURCE_ID,
)


@tracer.capture_method
def process_batch_event(event_detail: dict[str, Any]) -> None:
    """Validate and pass one actionable Batch event to the completion activity."""
    ccf_job_id = event_detail.get("jobName")
    batch_job_id = event_detail.get("jobId")
    batch_status_value = event_detail.get("status")
    if (
        not isinstance(ccf_job_id, str)
        or not isinstance(batch_job_id, str)
        or batch_status_value not in {status.value for status in BatchTerminalStatus}
    ):
        logger.info(
            "batch_job_event_ignored",
            extra={
                "job_id": ccf_job_id,
                "batch_job_id": batch_job_id,
                "batch_job_status": batch_status_value,
            },
        )
        return

    environment = event_detail.get("container", {}).get("environment", [])
    environment_values = {entry.get("name"): entry.get("value") for entry in environment if isinstance(entry, dict)}
    connector_id = environment_values.get("CUSTOM_CONNECTOR_FRAMEWORK_CUSTOM_CONNECTOR_ID")
    connector_arn_prefix = environment_values.get("CUSTOM_CONNECTOR_FRAMEWORK_CUSTOM_CONNECTOR_ARN_PREFIX")
    if not isinstance(connector_id, str) or not isinstance(connector_arn_prefix, str):
        logger.error(
            "batch_job_event_identity_missing",
            extra={"job_id": ccf_job_id, "batch_job_id": batch_job_id},
        )
        return

    tenant_context = TenantContext.from_arn_prefix(connector_arn_prefix)
    log_context = create_log_context(
        LogContext(
            connector_id=connector_id,
            account_id=tenant_context.account_id,
            job_id=ccf_job_id,
        )
    )
    terminal_status = complete_job_activity.complete(
        ReconcileBatchJobStatusRequest(
            tenant_context=tenant_context,
            connector_id=connector_id,
            job_id=ccf_job_id,
            batch_job_id=batch_job_id,
            batch_status=BatchTerminalStatus(batch_status_value),
        )
    )
    logger.info(
        "batch_job_event_reconciled",
        extra={
            **log_context,
            "batch_job_id": batch_job_id,
            "job_status": terminal_status.value,
        },
    )


@logger.inject_lambda_context(clear_state=True)
@tracer.capture_lambda_handler
def handler(event: dict[str, Any], _context: LambdaContext) -> dict[str, Any]:
    """Handle one EventBridge Batch state-change event."""
    process_batch_event(event.get("detail", {}))
    return {"statusCode": 200, "body": {"message": "Successfully processed job status change"}}
