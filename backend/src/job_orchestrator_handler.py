"""Lambda handler for orchestrating custom connector jobs."""

from dataclasses import dataclass
from http import HTTPStatus
from time import time
from typing import TYPE_CHECKING, Any, cast

import boto3
from aws_lambda_powertools.utilities.batch import BatchProcessor, EventType, process_partial_response
from aws_lambda_powertools.utilities.batch.types import PartialItemFailureResponse
from aws_lambda_powertools.utilities.data_classes.dynamo_db_stream_event import DynamoDBRecord
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.exceptions import ClientError
from ccf_api_models.models import JobStatus, ServiceExecution

from common.batch_job_definition import (
    BatchJobDefinitionCleanup,
    CleanupBatchJobDefinitionRequest,
    _is_exact_job_definition_arn,
)
from common.batch_submission import (
    BatchSubmissionRecoveryError,
    SubmittedExecutionIdentity,
    SubmittedExecutionLocator,
)
from common.env import (
    AWS_BATCH_JOB_QUEUE,
    CUSTOM_CONNECTOR_API_ENDPOINT,
    CUSTOM_CONNECTOR_JOBS_TABLE_NAME,
    CUSTOM_CONNECTORS_TABLE_NAME,
    FRAMEWORK_EXECUTION_ROLE_ARN,
)
from common.observability import LogContext, create_log_context, logger, tracer
from common.storage.ddb.custom_connector_jobs_dao import (
    BatchJobDefinitionRequest,
    BatchSubmissionIdentityRequest,
    ClaimStartOrchestrationRequest,
    CustomConnectorJobsDao,
    GetJobRequest,
    StartFailureCompensationRequest,
    StoredJob,
    UpdateJobStatusRequest,
)
from common.storage.ddb.custom_connectors_dao import CustomConnectorsDao, GetConnectorRequest
from common.tenant import TenantContext

if TYPE_CHECKING:
    from mypy_boto3_batch.type_defs import ContainerPropertiesTypeDef, KeyValuePairTypeDef

BATCH_OWNERSHIP_TAG_KEY = "ccf:owned-by"
BATCH_OWNERSHIP_TAG_VALUE = "custom-connector-framework"
_TERMINAL_JOB_STATUSES = frozenset({JobStatus.STOPPED, JobStatus.FAILED, JobStatus.SUCCEEDED})


@dataclass(frozen=True)
class JobStartContext:
    """Service-owned identity for starting one connector job."""

    job_id: str
    connector_id: str
    custom_connector_arn_prefix: str
    tenant_context: TenantContext


@dataclass(frozen=True)
class StartFailureState:
    """Resolved Batch identity and prior compensation state for one failed start."""

    batch_job_id: str | None
    job_definition_arn: str | None
    compensation_already_started: bool


def _submission_may_exist(error: Exception) -> bool:
    """Return whether a submission failure could still have created an execution."""
    if not isinstance(error, ClientError):
        return True
    status_code = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return not (
        isinstance(status_code, int) and HTTPStatus.BAD_REQUEST <= status_code < HTTPStatus.INTERNAL_SERVER_ERROR
    )


processor = BatchProcessor(event_type=EventType.DynamoDBStreams)
batch_client = boto3.client("batch")
dynamodb = boto3.resource("dynamodb")

connectors_table = dynamodb.Table(CUSTOM_CONNECTORS_TABLE_NAME)
jobs_table = dynamodb.Table(CUSTOM_CONNECTOR_JOBS_TABLE_NAME)

connectors_dao = CustomConnectorsDao(connectors_table)
jobs_dao = CustomConnectorJobsDao(jobs_table, connectors_dao, dynamodb.meta.client)
batch_job_definition_cleanup = BatchJobDefinitionCleanup(batch_client, jobs_dao)
submitted_execution_locator = SubmittedExecutionLocator(batch_client)


@tracer.capture_method
def record_handler(record: DynamoDBRecord) -> None:
    """Route one actionable DynamoDB job record."""
    if not record.dynamodb or not record.dynamodb.new_image:
        return

    new_image = record.dynamodb.new_image
    job_id = new_image.get("job_id")
    connector_id = new_image.get("connector_id")
    custom_connector_arn_prefix = new_image.get("custom_connector_arn_prefix")
    status = new_image.get("status")
    batch_job_id = new_image.get("batch_job_id")

    if (
        not isinstance(job_id, str)
        or not job_id
        or not isinstance(connector_id, str)
        or not connector_id
        or not isinstance(custom_connector_arn_prefix, str)
        or not custom_connector_arn_prefix
        or not isinstance(status, str)
        or not status
    ):
        logger.error(
            "job_record_identity_missing",
            extra={"job_id": job_id, "connector_id": connector_id, "status": status},
        )
        return

    tenant_context = TenantContext.from_arn_prefix(custom_connector_arn_prefix)
    log_context = create_log_context(
        LogContext(connector_id=connector_id, account_id=tenant_context.account_id, job_id=job_id)
    )
    logger.info(
        "job_record_received",
        extra={**log_context, "status": status, "event_name": record.event_name},
    )

    if status == JobStatus.STARTED.value:
        handle_job_start(
            JobStartContext(
                job_id=job_id,
                connector_id=connector_id,
                custom_connector_arn_prefix=custom_connector_arn_prefix,
                tenant_context=tenant_context,
            )
        )
    elif status == JobStatus.STOPPING.value:
        if batch_job_id is not None and not isinstance(batch_job_id, str):
            logger.error(
                "job_record_batch_identity_invalid",
                extra={"job_id": job_id, "connector_id": connector_id},
            )
            return
        handle_job_stop(job_id, connector_id, batch_job_id, tenant_context, log_context)
    else:
        logger.info("job_record_ignored", extra={**log_context, "status": status})


def _handle_job_start_replay(context: JobStartContext) -> None:
    """Retry persisted compensation or terminal definition cleanup."""
    log_context = create_log_context(
        LogContext(
            connector_id=context.connector_id,
            account_id=context.tenant_context.account_id,
            job_id=context.job_id,
        )
    )
    stored_job = jobs_dao.get_job(
        GetJobRequest(
            context.tenant_context,
            context.connector_id,
            context.job_id,
        )
    )
    if stored_job.status in _TERMINAL_JOB_STATUSES:
        batch_job_definition_cleanup.cleanup(
            CleanupBatchJobDefinitionRequest(
                context.tenant_context,
                context.connector_id,
                context.job_id,
                stored_job.batch_job_definition_arn,
            )
        )
        logger.info("job_start_orchestration_skipped", extra=log_context)
        return

    interrupted_start = stored_job.status is JobStatus.STARTED and stored_job.start_orchestration_claimed_at is not None
    if not interrupted_start:
        logger.info("job_start_orchestration_skipped", extra=log_context)
        return

    batch_job_id = stored_job.batch_job_id or _locate_lost_submission(stored_job, log_context)
    _compensate_start_failure(
        context,
        StartFailureState(
            batch_job_id=batch_job_id,
            job_definition_arn=stored_job.batch_job_definition_arn,
            compensation_already_started=stored_job.start_failure_compensation_started_at is not None
            and batch_job_id == stored_job.batch_job_id,
        ),
        log_context,
    )
    logger.info("job_start_orchestration_skipped", extra=log_context)


def _locate_lost_submission(
    stored_job: StoredJob,
    log_context: dict[str, Any],
) -> str | None:
    """Prove ownership of at most one execution whose SubmitJob response was lost."""
    if stored_job.batch_job_queue is None or stored_job.batch_job_name is None:
        return None
    if stored_job.batch_job_definition_arn is None:
        raise BatchSubmissionRecoveryError("Persisted Batch submission identity is incomplete")
    located_batch_job_id = submitted_execution_locator.locate(
        SubmittedExecutionIdentity(
            job_queue=stored_job.batch_job_queue,
            job_name=stored_job.batch_job_name,
            job_definition_arn=stored_job.batch_job_definition_arn,
        )
    )
    logger.info(
        "lost_batch_submission_located",
        extra={**log_context, "batch_job_id": located_batch_job_id},
    )
    return located_batch_job_id


def _compensate_start_failure(
    context: JobStartContext,
    state: StartFailureState,
    log_context: dict[str, Any],
) -> None:
    """Persist compensation, terminate any owned execution, then fail the job."""
    if not state.compensation_already_started:
        jobs_dao.mark_start_failure_compensation(
            StartFailureCompensationRequest(
                tenant_context=context.tenant_context,
                connector_id=context.connector_id,
                job_id=context.job_id,
                batch_job_id=state.batch_job_id,
                job_definition_arn=state.job_definition_arn,
            )
        )
    if state.batch_job_id is not None:
        batch_client.terminate_job(
            jobId=state.batch_job_id,
            reason="CCF could not persist the submitted Batch execution",
        )
    jobs_dao.update_job_status(
        UpdateJobStatusRequest(
            tenant_context=context.tenant_context,
            connector_id=context.connector_id,
            job_id=context.job_id,
            status=JobStatus.FAILED,
            batch_job_id=state.batch_job_id,
        )
    )
    batch_job_definition_cleanup.cleanup(
        CleanupBatchJobDefinitionRequest(
            context.tenant_context,
            context.connector_id,
            context.job_id,
            state.job_definition_arn,
        )
    )
    logger.info("job_start_compensated", extra=log_context)


@tracer.capture_method
def handle_job_start(context: JobStartContext) -> None:
    """Register and submit one connector job with service-owned execution values."""
    log_context = create_log_context(
        LogContext(
            connector_id=context.connector_id,
            account_id=context.tenant_context.account_id,
            job_id=context.job_id,
        )
    )
    if not jobs_dao.claim_start_orchestration(
        ClaimStartOrchestrationRequest(
            tenant_context=context.tenant_context,
            connector_id=context.connector_id,
            job_id=context.job_id,
        )
    ):
        _handle_job_start_replay(context)
        return

    batch_job_id: str | None = None
    job_definition_arn: str | None = None
    submission_identity: SubmittedExecutionIdentity | None = None
    try:
        connector = connectors_dao.get_connector(
            GetConnectorRequest(
                tenant_context=context.tenant_context,
                connector_id=context.connector_id,
            )
        )
        container_properties = connector.container_properties
        runtime_role_arn = connector.runtime_role_arn
        if runtime_role_arn is None:
            raise ValueError("Connector has no service-managed runtime role")
        environment: list[KeyValuePairTypeDef] = [
            {"name": "CUSTOM_CONNECTOR_JOB_ID", "value": context.job_id},
            {
                "name": "CUSTOM_CONNECTOR_FRAMEWORK_CUSTOM_CONNECTOR_ID",
                "value": context.connector_id,
            },
            {
                "name": "CUSTOM_CONNECTOR_FRAMEWORK_CUSTOM_CONNECTOR_ARN_PREFIX",
                "value": context.custom_connector_arn_prefix,
            },
            {"name": "CUSTOM_CONNECTOR_FRAMEWORK_API_ENDPOINT", "value": CUSTOM_CONNECTOR_API_ENDPOINT or ""},
            {"name": "AWS_REGION", "value": context.tenant_context.region},
        ]
        batch_container_properties: ContainerPropertiesTypeDef = {
            "image": container_properties.image_uri,
            "executionRoleArn": FRAMEWORK_EXECUTION_ROLE_ARN,
            "jobRoleArn": runtime_role_arn,
            "command": [command.root for command in container_properties.command],
            "environment": environment,
            "resourceRequirements": [
                {"type": "VCPU", "value": str(container_properties.resource_requirements.cpu)},
                {"type": "MEMORY", "value": str(container_properties.resource_requirements.memory)},
            ],
        }
        register_response = batch_client.register_job_definition(
            jobDefinitionName=context.job_id,
            type="container",
            containerProperties=batch_container_properties,
            platformCapabilities=["FARGATE"],
            tags={
                "connector_id": context.connector_id,
                "custom_connector_arn_prefix": context.custom_connector_arn_prefix,
                "job_id": context.job_id,
            },
        )
        registered_job_definition_arn = register_response.get("jobDefinitionArn")
        if not _is_exact_job_definition_arn(
            registered_job_definition_arn,
            context.job_id,
        ):
            raise ValueError("Batch registration returned an ambiguous job-definition ARN")
        job_definition_arn = registered_job_definition_arn
        jobs_dao.store_batch_job_definition(
            BatchJobDefinitionRequest(
                context.tenant_context,
                context.connector_id,
                context.job_id,
                job_definition_arn,
            )
        )
        job_queue = cast(str, AWS_BATCH_JOB_QUEUE)
        jobs_dao.store_batch_submission_identity(
            BatchSubmissionIdentityRequest(
                tenant_context=context.tenant_context,
                connector_id=context.connector_id,
                job_id=context.job_id,
                job_queue=job_queue,
                job_name=context.job_id,
            )
        )
        submission_identity = SubmittedExecutionIdentity(
            job_queue=job_queue,
            job_name=context.job_id,
            job_definition_arn=job_definition_arn,
        )
        submit_response = batch_client.submit_job(
            jobName=context.job_id,
            jobQueue=job_queue,
            jobDefinition=job_definition_arn,
            timeout={"attemptDurationSeconds": container_properties.timeout},
            tags={BATCH_OWNERSHIP_TAG_KEY: BATCH_OWNERSHIP_TAG_VALUE},
        )
        batch_job_id = submit_response["jobId"]
        jobs_dao.update_job_status(
            UpdateJobStatusRequest(
                tenant_context=context.tenant_context,
                connector_id=context.connector_id,
                job_id=context.job_id,
                status=JobStatus.RUNNING,
                batch_job_id=batch_job_id,
                service_execution=ServiceExecution(
                    execution_id=batch_job_id,
                    execution_role_arn=FRAMEWORK_EXECUTION_ROLE_ARN,
                    job_role_arn=runtime_role_arn,
                    started_at=time(),
                ),
            )
        )
        logger.info(
            "batch_job_started",
            extra={**log_context, "batch_job_id": batch_job_id},
        )
    except Exception as error:
        logger.error(
            "job_start_failed",
            extra={
                **log_context,
                "batch_job_id": batch_job_id,
                "error_type": type(error).__name__,
            },
        )
        if batch_job_id is None and submission_identity is not None and _submission_may_exist(error):
            try:
                batch_job_id = submitted_execution_locator.locate(submission_identity)
            except BatchSubmissionRecoveryError as recovery_error:
                logger.error(
                    "lost_batch_submission_unverified",
                    extra={**log_context, "error_type": type(recovery_error).__name__},
                )
                raise recovery_error from error
        jobs_dao.mark_start_failure_compensation(
            StartFailureCompensationRequest(
                tenant_context=context.tenant_context,
                connector_id=context.connector_id,
                job_id=context.job_id,
                batch_job_id=batch_job_id,
                job_definition_arn=job_definition_arn,
            )
        )
        if batch_job_id is not None:
            try:
                batch_client.terminate_job(
                    jobId=batch_job_id,
                    reason="CCF could not persist the submitted Batch execution",
                )
            except Exception as termination_error:
                logger.error(
                    "untracked_batch_job_cancel_failed",
                    extra={
                        **log_context,
                        "batch_job_id": batch_job_id,
                        "error_type": type(termination_error).__name__,
                    },
                )
                raise termination_error from error
        try:
            jobs_dao.update_job_status(
                UpdateJobStatusRequest(
                    tenant_context=context.tenant_context,
                    connector_id=context.connector_id,
                    job_id=context.job_id,
                    status=JobStatus.FAILED,
                    batch_job_id=batch_job_id,
                )
            )
        except Exception as update_error:
            logger.error(
                "job_failure_status_update_failed",
                extra={**log_context, "error_type": type(update_error).__name__},
            )
            raise update_error from error
        batch_job_definition_cleanup.cleanup(
            CleanupBatchJobDefinitionRequest(
                context.tenant_context,
                context.connector_id,
                context.job_id,
                job_definition_arn,
            )
        )


@tracer.capture_method
def handle_job_stop(
    job_id: str,
    connector_id: str,
    batch_job_id: str | None,
    tenant_context: TenantContext,
    log_context: dict[str, Any] | None = None,
) -> None:
    """Request cancellation of one running Batch job."""
    if log_context is None:
        log_context = create_log_context(
            LogContext(
                connector_id=connector_id,
                account_id=tenant_context.account_id,
                job_id=job_id,
            )
        )
    stored_job = jobs_dao.get_job(GetJobRequest(tenant_context, connector_id, job_id))
    job_definition_arn = stored_job.batch_job_definition_arn
    if stored_job.status in _TERMINAL_JOB_STATUSES:
        batch_job_definition_cleanup.cleanup(
            CleanupBatchJobDefinitionRequest(
                tenant_context,
                connector_id,
                job_id,
                job_definition_arn,
            )
        )
        logger.info("job_stop_orchestration_skipped", extra=log_context)
        return

    if not batch_job_id:
        logger.info("batch_job_stopped_before_submission", extra=log_context)
        jobs_dao.update_job_status(
            UpdateJobStatusRequest(
                tenant_context=tenant_context,
                connector_id=connector_id,
                job_id=job_id,
                status=JobStatus.STOPPED,
            )
        )
        batch_job_definition_cleanup.cleanup(
            CleanupBatchJobDefinitionRequest(
                tenant_context,
                connector_id,
                job_id,
                job_definition_arn,
            )
        )
        return

    try:
        batch_client.terminate_job(
            jobId=batch_job_id,
            reason="Job stopped by user via Custom Connector Framework API",
        )
        logger.info(
            "batch_job_cancellation_requested",
            extra={**log_context, "batch_job_id": batch_job_id},
        )
    except Exception as error:
        logger.error(
            "batch_job_cancel_failed",
            extra={
                **log_context,
                "batch_job_id": batch_job_id,
                "error_type": type(error).__name__,
            },
        )
        raise


@logger.inject_lambda_context
@tracer.capture_lambda_handler
def handler(event: dict[str, Any], context: LambdaContext) -> PartialItemFailureResponse:
    """Process a DynamoDB Streams batch with partial failure reporting."""
    logger.info(
        "job_stream_batch_received",
        extra={"record_count": len(event.get("Records", []))},
    )
    try:
        return process_partial_response(
            event=event,
            record_handler=record_handler,
            processor=processor,
            context=context,
        )
    except Exception as error:
        logger.error(
            "job_stream_event_failed",
            extra={"error_type": type(error).__name__},
        )
        raise
