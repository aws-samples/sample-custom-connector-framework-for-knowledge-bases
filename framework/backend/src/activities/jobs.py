"""One activity per job operation; `ListResources` over the jobs DAO lists a custom connector's jobs.

Starting a job writes it `STARTED`, sets its custom connector `IN_USE`, and requires the
destination `ACTIVE`, in one transaction, then starts the job's `CcfJobExecution` execution, named
by the job ID. Stopping a job sets it `STOPPING`, stops the execution (Step Functions stops the
Batch job), and ends the job `STOPPED`.
"""

from __future__ import annotations

from typing import ClassVar

from aws_lambda_powertools.metrics import MetricUnit
from ccf_api_models.base import ValueModel
from ccf_api_models.identifiers import ConnectorRef, GeneratedIdKind, JobRef
from ccf_api_models.models import (
    GetCustomConnectorJobRequest,
    GetCustomConnectorJobResponseContent,
    JobStatus,
    StartCustomConnectorJobRequest,
    StartCustomConnectorJobResponseContent,
    StopCustomConnectorJobRequest,
)

from common.errors import DependencyThrottledError, DependencyUnavailableError
from common.observability import LogContext, MetricDimensionName, MetricName, MetricRecord, ServiceMetrics, logger
from common.operation import OperationRequest
from common.resource_data_codec import DecryptResourceDataRequest, EncryptResourceDataRequest, ResourceDataCodec
from common.resource_ids import ResourceIds
from common.storage.ddb.custom_connector_jobs_dao import (
    ConnectorInUseError,
    CustomConnectorJobsDao,
    JobItem,
    StartJobRecord,
)
from common.validators.byte_limits import FieldOfModel, Utf8ByteLimit
from common.workflow_executions import ExistingExecution, WorkflowExecutions

_CONFIGURATION_LIMIT = Utf8ByteLimit.of(
    FieldOfModel(model=StartCustomConnectorJobRequest, field_name="job_configuration")
)


class JobExecutionInput(ValueModel):
    """What `CcfJobExecution` reads; a retried start names the execution the first one started."""

    EXISTING_EXECUTION: ClassVar[ExistingExecution] = ExistingExecution.REUSE

    tenant_arn_prefix: str
    destination_id: str
    connector_id: str
    job_id: str
    correlation_id: str

    @property
    def execution_name(self) -> str:
        return self.job_id


class StartCustomConnectorJobActivity:
    """Checks the configuration's size, encrypts it, makes the start transaction, and starts the execution."""

    def __init__(
        self, jobs: CustomConnectorJobsDao, codec: ResourceDataCodec, executions: WorkflowExecutions, ids: ResourceIds
    ) -> None:
        self._jobs = jobs
        self._codec = codec
        self._executions = executions
        self._ids = ids

    def start(
        self, request: OperationRequest[StartCustomConnectorJobRequest]
    ) -> StartCustomConnectorJobResponseContent:
        """Return the `STARTED` job; a failed execution start undoes the transaction and raises its error."""
        configuration = request.payload.job_configuration
        if configuration is not None:
            _CONFIGURATION_LIMIT.check(configuration)
        job = JobRef(connector=ConnectorRef.of(request), job_id=self._ids.new(GeneratedIdKind.JOB))
        encrypted = (
            None
            if configuration is None
            else self._codec.encrypt(EncryptResourceDataRequest(data=configuration, resource_arn=job.arn))
        )
        record = StartJobRecord(job=job, encrypted_configuration=encrypted)
        try:
            item = self._jobs.start_job(record)
        except ConnectorInUseError as error:
            self._stop_if_abandoned(error)
            item = self._jobs.start_job(record)
        connector = job.connector
        execution = JobExecutionInput(
            tenant_arn_prefix=connector.tenant.arn_prefix,
            destination_id=connector.destination_id,
            connector_id=connector.connector_id,
            job_id=job.job_id,
            correlation_id=request.trace.correlation_id,
        )
        try:
            self._executions.start(execution)
        except DependencyUnavailableError, DependencyThrottledError:
            self._undo_start(job)
            raise
        return StartCustomConnectorJobResponseContent(job=item.to_summary())

    def _stop_if_abandoned(self, in_use: ConnectorInUseError) -> None:
        """Stop the holding job when its execution has ended without recording it; re-raise while it runs."""
        holder = in_use.holder
        if self._executions.is_running(self._executions.execution_arn(holder.job_id)):
            raise in_use
        logger.warning("abandoned_job_stopped", extra=_log_fields(holder))
        self._jobs.request_stop(holder)
        self._jobs.finish_stopped(holder)

    def _undo_start(self, job: JobRef) -> None:
        """Delete the unstarted job; a failure here is logged, not raised over the start's error."""
        try:
            self._jobs.cancel_start(job)
        except Exception as error:
            logger.error(
                "job_start_not_undone",
                extra={**_log_fields(job), "error_type": type(error).__name__},
            )


class GetCustomConnectorJobActivity:
    """Returns one job of the custom connector, with its decrypted configuration."""

    def __init__(self, jobs: CustomConnectorJobsDao, codec: ResourceDataCodec) -> None:
        self._jobs = jobs
        self._codec = codec

    def fetch(self, request: OperationRequest[GetCustomConnectorJobRequest]) -> GetCustomConnectorJobResponseContent:
        item = self._jobs.get_job(JobRef.of(request))
        ciphertext = item.encrypted_configuration
        configuration = (
            None
            if ciphertext is None
            else self._codec.decrypt(DecryptResourceDataRequest(ciphertext=ciphertext, resource_arn=item.arn))
        )
        return GetCustomConnectorJobResponseContent(job=item.to_job(configuration))


class StopCustomConnectorJobActivity:
    """Sets a job that hasn't ended `STOPPING`, stops its execution, and ends it `STOPPED`."""

    def __init__(self, jobs: CustomConnectorJobsDao, executions: WorkflowExecutions, metrics: ServiceMetrics) -> None:
        self._jobs = jobs
        self._executions = executions
        self._metrics = metrics

    def stop(self, request: OperationRequest[StopCustomConnectorJobRequest]) -> None:
        """A job that already ended conflicts; the metrics are published only by the call that ends the job."""
        job = JobRef.of(request)
        stopping = self._jobs.request_stop(job)
        self._executions.stop(job.job_id)
        if self._jobs.finish_stopped(job):
            self._publish(stopping)

    def _publish(self, stopping: JobItem) -> None:
        connector = {MetricDimensionName.CONNECTOR_ID: stopping.connector_id}
        status = {MetricDimensionName.STATUS: JobStatus.STOPPED.value}
        for dimensions in (status, {**status, **connector}):
            self._metrics.record(MetricRecord(name=MetricName.JOBS_COMPLETED, value=1, dimensions=dimensions))
        stopped_at = stopping.stop_requested_at or stopping.updated_at
        self._metrics.record(
            MetricRecord(
                name=MetricName.JOB_DURATION,
                value=max(stopped_at - stopping.created_at, 0.0),
                unit=MetricUnit.Seconds,
                dimensions=connector,
            )
        )


def _log_fields(job: JobRef) -> dict[str, str]:
    connector = job.connector
    return LogContext(
        destination_id=connector.destination_id,
        connector_id=connector.connector_id,
        account_id=connector.tenant.account_id,
        job_id=job.job_id,
    ).as_log_extra()
