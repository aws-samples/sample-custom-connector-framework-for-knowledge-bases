"""The job runner: read the inputs, open source access, sweep, run the implementation once, flush, and report.

`JobExitCode` gives the exit code of each failure. A failure before `run()`
ends the job without calling it. A failure in `run()` still sends the queued puts, then the
queued deletes, so the framework keeps every file the job wrote. When a successful run's final
send fails, the job still reports and exits 76 `FrameworkApiUnavailable`.
"""

from __future__ import annotations

import logging

from ccf_api_models.base import ValueModel
from ccf_api_models.identifiers import CollectionIdentity, ConnectorRef, DestinationRef, TenantContext
from ccf_api_models.job_container import JobExitCode
from ccf_api_models.models import Destination
from ccf_api_models.operations import CcfApi
from pydantic import ConfigDict

from custom_connector_framework.client.pages import Pages
from custom_connector_framework.client.send_deadline import SendDeadline
from custom_connector_framework.implementation import ConnectorImplementation
from custom_connector_framework.job_context import JobContext
from custom_connector_framework.runner.exit_codes import JobStartError
from custom_connector_framework.runner.file_sweeper import FileSweeper, SweepRequest
from custom_connector_framework.runner.job_inputs_reader import (
    FrameworkApiUnavailableError,
    JobIdentity,
    JobInputsReader,
)
from custom_connector_framework.runner.secret_reader import ReadSecretRequest, SecretReader
from custom_connector_framework.runner.source_access import OpenSourceAccessRequest, SourceAccess
from custom_connector_framework.runner.termination_signal import JobTerminatedError, TerminationSignal
from custom_connector_framework.store.content_reader import ContentReader
from custom_connector_framework.store.destination_formats import DestinationFormats
from custom_connector_framework.store.destination_objects import DestinationObjects
from custom_connector_framework.store.document_store import DocumentStore
from custom_connector_framework.store.file_writer import FileWriter
from custom_connector_framework.store.identifiers import StoreTarget
from custom_connector_framework.store.media_types import MediaTypes
from custom_connector_framework.store.models import ReportCounts, StoreReport
from custom_connector_framework.store.record_queues import RecordQueues
from custom_connector_framework.store.report import StoreCounters

logger = logging.getLogger(__name__)

FLUSH_SECONDS_AFTER_TERMINATION = 25.0
"""How long the flush after `SIGTERM` may send for; Batch kills the container 30 seconds after the signal."""


class JobOutcome(ValueModel):
    """How the job ended, and the store's final report; the report is empty when the job failed to start."""

    exit_code: JobExitCode
    report: StoreReport


class _StartedJob(ValueModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    implementation: type[ConnectorImplementation]
    job: JobContext
    store: DocumentStore


class _FinishedRun(ValueModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    store: DocumentStore
    exit_code: JobExitCode
    """How `run()` ended, before the flush."""


class JobRunner:
    """Runs one job and returns its exit code and the store's report."""

    def __init__(
        self,
        identity: JobIdentity,
        inputs: JobInputsReader,
        source_access: SourceAccess,
        secrets: SecretReader,
        api: CcfApi,
        pages: Pages,
        sweeper: FileSweeper,
        termination: TerminationSignal,
        send_deadline: SendDeadline,
    ) -> None:
        self._identity = identity
        self._inputs = inputs
        self._source_access = source_access
        self._secrets = secrets
        self._api = api
        self._pages = pages
        self._sweeper = sweeper
        self._termination = termination
        self._send_deadline = send_deadline
        """The framework client's send deadline, started when `SIGTERM` stops `run()`."""

    def run(self, implementation: type[ConnectorImplementation]) -> JobOutcome:
        try:
            started = self._start(implementation)
        except JobStartError as error:
            logger.error("job_start_failed", extra={"exit_code": error.exit_code.value}, exc_info=error)
            return JobOutcome(exit_code=error.exit_code, report=StoreReport(collections=(), total=ReportCounts()))
        finished = _FinishedRun(store=started.store, exit_code=self._run_implementation(started))
        try:
            exit_code = self._flush(finished)
        finally:
            self._report(finished)
        return JobOutcome(exit_code=exit_code, report=finished.store.get_report())

    def _start(self, implementation: type[ConnectorImplementation]) -> _StartedJob:
        identity = self._identity
        inputs = self._inputs.read(identity)
        source_access = inputs.connector.source_access
        source_session = self._source_access.open(
            OpenSourceAccessRequest(
                role_arn=source_access.role_arn,
                external_id=source_access.external_id,
                job_id=identity.job_id,
                region=identity.region,
            )
        )
        secret = (
            None
            if source_access.secret_arn is None
            else self._secrets.read(
                ReadSecretRequest(source_session=source_session, secret_arn=source_access.secret_arn)
            )
        )
        objects = DestinationObjects(source_session.client("s3"), _bucket_name(inputs.destination))
        target = StoreTarget(
            tenant_arn_prefix=identity.tenant_arn_prefix,
            destination_id=identity.destination_id,
            connector_id=identity.connector_id,
        )
        counters = StoreCounters()
        store = DocumentStore(
            target=target,
            api=self._api,
            pages=self._pages,
            destination_format=DestinationFormats().select(inputs.destination),
            media_types=MediaTypes(),
            reader=ContentReader(),
            files=FileWriter(objects),
            queues=RecordQueues(self._api, target, counters),
            counters=counters,
        )
        self._sweeper.sweep(
            SweepRequest(
                connector=ConnectorRef(
                    destination=DestinationRef(
                        tenant=TenantContext.from_arn_prefix(identity.tenant_arn_prefix),
                        destination_id=identity.destination_id,
                    ),
                    connector_id=identity.connector_id,
                ),
                objects=objects,
            )
        )
        job = JobContext(
            destination_id=identity.destination_id,
            connector_id=identity.connector_id,
            job_id=identity.job_id,
            connector_configuration=inputs.connector.connector_configuration,
            job_configuration=inputs.job.job_configuration,
            secret=secret,
            source_session=source_session,
        )
        return _StartedJob(implementation=implementation, job=job, store=store)

    def _run_implementation(self, started: _StartedJob) -> JobExitCode:
        self._termination.arm()
        try:
            started.implementation(started.job, started.store).run()
        except JobTerminatedError:
            logger.warning("job_terminated")
            return JobExitCode.JOB_TERMINATED
        except Exception:
            logger.exception("connector_implementation_failed")
            return JobExitCode.CONNECTOR_IMPLEMENTATION_ERROR
        finally:
            self._termination.disarm()
        return JobExitCode.SUCCEEDED

    def _flush(self, finished: _FinishedRun) -> JobExitCode:
        """Send the queued records and return the job's exit code.

        A failed send after a successful run makes the job fail with 76; after a failed run it is
        logged and the run's exit code stands.
        """
        match finished.exit_code:
            case JobExitCode.SUCCEEDED:
                return self._flush_after_success(finished.store)
            case JobExitCode.JOB_TERMINATED:
                self._send_deadline.start(FLUSH_SECONDS_AFTER_TERMINATION)
                return self._flush_after_failure(finished)
            case _:
                return self._flush_after_failure(finished)

    @staticmethod
    def _flush_after_success(store: DocumentStore) -> JobExitCode:
        try:
            store.flush()
        except Exception:
            logger.exception("queued_records_not_sent")
            return JobExitCode.FRAMEWORK_API_UNAVAILABLE
        return JobExitCode.SUCCEEDED

    @staticmethod
    def _flush_after_failure(finished: _FinishedRun) -> JobExitCode:
        try:
            finished.store.flush_after_failure()
        except Exception:
            logger.exception("queued_records_not_sent")
        return finished.exit_code

    @staticmethod
    def _report(finished: _FinishedRun) -> None:
        report = finished.store.get_report()
        # Keyed by framework collection ID, so no source collection ID reaches the log.
        logger.info(
            "job_reported",
            extra={
                "exit_code": finished.exit_code.value,
                "report": {
                    "total": report.total.model_dump(),
                    "collections": {
                        CollectionIdentity.of(collection.collection_id).collection_id: collection.counts.model_dump()
                        for collection in report.collections
                    },
                },
            },
        )


def _bucket_name(destination: Destination) -> str:
    bucket_name = destination.destination_resources.bucket_name
    if bucket_name is None:
        raise FrameworkApiUnavailableError("The destination has no bucket")
    return bucket_name
