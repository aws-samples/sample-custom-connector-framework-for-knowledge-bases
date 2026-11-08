"""Tenant-scoped DynamoDB records of custom connector jobs (`CcfCustomConnectorJobs`).

Jobs are keyed by tenant ARN prefix and job ID. The `ByConnectorSummary` index keys them by
`connector_key` (the custom connector ARN) and `created_at`, so a listing is newest first. Every
write sets `ttl`: `record_retention_seconds` out for a job in a terminal status, and
`active_record_retention_seconds` for any other, so a record outlives the Batch job that runs it.
Each status change is one condition expression, so the conditions are the allowed transitions.
The custom connector's reservation and release are halves of the job's transactions, so they
are written here too.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from ccf_api_models.base import ValueModel
from ccf_api_models.contracts import TablesContract
from ccf_api_models.identifiers import ConnectorRef, JobRef
from ccf_api_models.models import ConnectorStatus, DestinationStatus, Job, JobStatus, JobSummary, ServiceExecution
from pydantic import BaseModel, ConfigDict, SecretStr

from common.aws_error_code import AwsErrorCode
from common.errors import CancellationReason, ResourceConflictError, ResourceNotFoundError, TransactionCanceledError
from common.resource_ids import Clock
from common.settings import JobSettings
from common.storage.ddb.model_table import (
    ConditionCheckRequest,
    DeleteItemRequest,
    ItemKey,
    ListItemsRequest,
    ModelTable,
    OnConditionFailure,
    Page,
    PutItemRequest,
    QueryRequest,
    SortOrder,
    TransactionRequest,
    TransactWrite,
    UpdateItemRequest,
)

BY_CONNECTOR_INDEX = "ByConnectorSummary"
"""Keys jobs by `connector_key` and `created_at`, projecting only the `JobSummary` members."""
TERMINAL_STATUSES = frozenset({JobStatus.STOPPED, JobStatus.FAILED, JobStatus.SUCCEEDED})

_FAILED = AwsErrorCode.CONDITIONAL_CHECK_FAILED_REASON
_NAMES = {"#status": "status", "#ttl": "ttl"}
_OWNED = "destination_id = :destination_id AND connector_id = :connector_id"


class ConnectorInUseError(ResourceConflictError):
    """A job can't start because another job of the custom connector holds it."""

    def __init__(self, message: str, *, holder: JobRef) -> None:
        super().__init__(message)
        self.holder = holder
        """The job that holds the custom connector."""


class JobItem(JobSummary):
    """A stored job: its keys, its summary members, and the members only the backend reads."""

    model_config = ConfigDict(extra="ignore", frozen=True, val_json_bytes="base64")

    tenant_arn_prefix: str
    connector_key: str
    destination_id: str
    connector_id: str
    ttl: int
    status_reason: str | None = None
    encrypted_configuration: bytes | None = None
    service_execution: ServiceExecution | None = None
    stop_requested_at: float | None = None

    def to_summary(self) -> JobSummary:
        return JobSummary.model_validate(self.model_dump(include=set(JobSummary.model_fields)))

    def to_job(self, configuration: SecretStr | None) -> Job:
        return Job(
            **self.to_summary().model_dump(),
            status_reason=self.status_reason,
            job_configuration=configuration,
            service_execution=self.service_execution,
        )


class _SummaryItem(JobSummary):
    model_config = ConfigDict(extra="ignore", frozen=True)


class _OtherItem(BaseModel):
    """A custom connector or destination item, which this DAO only writes or checks inside a transaction."""

    model_config = ConfigDict(extra="ignore", frozen=True)


class StartJobRecord(ValueModel):
    """A new job and its encrypted configuration, when it has one."""

    job: JobRef
    encrypted_configuration: bytes | None = None


class JobsTables(ValueModel):
    """The boto3 jobs, custom connectors, and destinations tables, their client, the job settings, and the clock."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    jobs_table: Any
    connectors_table: Any
    destinations_table: Any
    client: Any
    settings: JobSettings
    clock: Clock


class _Tables(ValueModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    jobs: ModelTable[JobItem]
    summaries: ModelTable[_SummaryItem]
    connectors: ModelTable[_OtherItem]
    destinations: ModelTable[_OtherItem]


class _StartReasons(ValueModel):
    """The start transaction's cancellation reasons, in its order: the job, the custom connector, the destination."""

    requested: JobRef
    job: CancellationReason
    connector: CancellationReason
    destination: CancellationReason

    def error(self) -> ResourceConflictError | ResourceNotFoundError:
        match self.connector:
            case CancellationReason(code=AwsErrorCode.CONDITIONAL_CHECK_FAILED_REASON, item=None):
                return ResourceNotFoundError("Custom connector does not exist")
            case CancellationReason(
                item={"status": ConnectorStatus.IN_USE.value, "active_job_id": str(holder_job_id)}
            ) if self.destination.code is None:
                return ConnectorInUseError(
                    "Custom connector is in use by another job",
                    holder=JobRef(connector=self.requested.connector, job_id=holder_job_id),
                )
        return ResourceConflictError("Custom connector is in use or its destination is not ACTIVE")


class CustomConnectorJobsDao:
    """Reads and writes job records; every single-item read is strongly consistent."""

    def __init__(self, tables: _Tables, settings: JobSettings, clock: Clock) -> None:
        self._tables = tables
        self._settings = settings
        self._clock = clock

    @classmethod
    def over(cls, request: JobsTables) -> CustomConnectorJobsDao:
        contracts = TablesContract.load()
        tables = _Tables(
            jobs=ModelTable(request.jobs_table, request.client, JobItem, contracts.jobs),
            summaries=ModelTable(request.jobs_table, request.client, _SummaryItem, contracts.jobs),
            connectors=ModelTable(request.connectors_table, request.client, _OtherItem, contracts.connectors),
            destinations=ModelTable(request.destinations_table, request.client, _OtherItem, contracts.destinations),
        )
        return cls(tables, request.settings, request.clock)

    def start_job(self, record: StartJobRecord) -> JobItem:
        """Save a `STARTED` job, set its custom connector `IN_USE`, and require its destination `ACTIVE`, at once."""
        item = self._new_item(record)
        connector = record.job.connector
        tables = self._tables
        reserve = UpdateItemRequest(
            key=_connector_key(connector),
            action="Job start",
            expression="SET #status = :in_use, active_job_id = :job_id, updated_at = :now ADD #version :one",
            condition="#status = :available",
            names={"#status": "status", "#version": "version"},
            values={
                ":in_use": ConnectorStatus.IN_USE.value,
                ":available": ConnectorStatus.AVAILABLE.value,
                ":job_id": record.job.job_id,
                ":now": Decimal(str(item.created_at)),
                ":one": 1,
            },
            on_condition_failure=OnConditionFailure.ALL_OLD,
        )
        destination_active = ConditionCheckRequest(
            key=ItemKey(partition=connector.tenant.arn_prefix, sort=connector.destination_id),
            condition="#status = :active",
            names={"#status": "status"},
            values={":active": DestinationStatus.ACTIVE.value},
        )
        writes = (
            tables.jobs.put_action(
                PutItemRequest(item=item, action="Job start", condition="attribute_not_exists(job_id)")
            ),
            tables.connectors.update_action(reserve),
            tables.destinations.condition_check(destination_active),
        )
        try:
            tables.jobs.transact(TransactionRequest(action="Job start", writes=writes))
        except TransactionCanceledError as error:
            job, connector_reason, destination = error.reasons
            raise _StartReasons(
                requested=record.job, job=job, connector=connector_reason, destination=destination
            ).error() from error
        return item

    def cancel_start(self, job: JobRef) -> None:
        """Delete a `STARTED` job and release its custom connector, in one transaction."""
        tables = self._tables
        delete = DeleteItemRequest(
            key=_key(job),
            action="Job start undo",
            condition="#status = :started",
            names={"#status": "status"},
            values={":started": JobStatus.STARTED.value},
        )
        writes = (tables.jobs.delete_action(delete), self._release(job))
        tables.jobs.transact(TransactionRequest(action="Job start undo", writes=writes))

    def get_job(self, job: JobRef) -> JobItem:
        """The job, or ResourceNotFoundError when it belongs to another custom connector or destination."""
        item = self._tables.jobs.get(_key(job))
        owner = job.connector
        if item is None or (item.destination_id, item.connector_id) != (owner.destination_id, owner.connector_id):
            raise ResourceNotFoundError("Job does not exist")
        return item

    def list(self, request: ListItemsRequest[ConnectorRef]) -> Page[JobSummary]:
        """One page of the custom connector's jobs, newest first; the filter value is a status."""
        status = request.filter_value
        page = self._tables.summaries.query(
            QueryRequest(
                partition_value=request.parent.arn,
                index=BY_CONNECTOR_INDEX,
                filter_equals={} if status is None else {"status": status},
                max_results=request.max_results,
                next_token=request.next_token,
                order=SortOrder.DESCENDING,
            )
        )
        return Page(
            items=[JobSummary.model_validate(item.model_dump()) for item in page.items], next_token=page.next_token
        )

    def request_stop(self, job: JobRef) -> JobItem:
        """Set a job that hasn't ended `STOPPING`; a repeated stop writes `stop_requested_at` again."""
        now = self._clock.now()
        try:
            return self._tables.jobs.update(
                UpdateItemRequest(
                    key=_key(job),
                    action="Job stop",
                    expression="SET #status = :stopping, stop_requested_at = :now, updated_at = :now, #ttl = :ttl",
                    condition=f"{_OWNED} AND #status IN (:started, :running, :stopping)",
                    names=_NAMES,
                    values={
                        ":stopping": JobStatus.STOPPING.value,
                        ":started": JobStatus.STARTED.value,
                        ":running": JobStatus.RUNNING.value,
                        ":now": now,
                        ":ttl": int(now) + self._settings.active_record_retention_seconds,
                        **_owner(job),
                    },
                )
            )
        except ResourceConflictError:
            self.get_job(job)
            raise

    def finish_stopped(self, job: JobRef) -> bool:
        """Set a `STOPPING` job `STOPPED` and release its custom connector; False when the job already ended.

        A custom connector another job holds is left alone, and the job still ends `STOPPED`.
        """
        stopped = self._stopped(job)
        try:
            self._tables.jobs.transact(TransactionRequest(action="Job stop", writes=(stopped, self._release(job))))
        except TransactionCanceledError as error:
            job_reason, connector_reason = error.reasons
            if job_reason.code is _FAILED:
                return False
            if connector_reason.code is not _FAILED:
                raise
            self._tables.jobs.transact(TransactionRequest(action="Job stop", writes=(stopped,)))
        return True

    def _stopped(self, job: JobRef) -> TransactWrite:
        now = self._clock.now()
        return self._tables.jobs.update_action(
            UpdateItemRequest(
                key=_key(job),
                action="Job stop",
                expression="SET #status = :stopped, updated_at = :now, #ttl = :ttl",
                condition=f"{_OWNED} AND #status = :stopping",
                names=_NAMES,
                values={
                    ":stopped": JobStatus.STOPPED.value,
                    ":stopping": JobStatus.STOPPING.value,
                    ":now": now,
                    ":ttl": int(now) + self._settings.record_retention_seconds,
                    **_owner(job),
                },
            )
        )

    def _release(self, job: JobRef) -> TransactWrite:
        """Return the job's `IN_USE` custom connector to `AVAILABLE`, only while this job holds it."""
        return self._tables.connectors.update_action(
            UpdateItemRequest(
                key=_connector_key(job.connector),
                action="Custom connector release",
                expression="SET #status = :available, updated_at = :now REMOVE active_job_id ADD #version :one",
                condition="#status = :in_use AND active_job_id = :job_id",
                names={"#status": "status", "#version": "version"},
                values={
                    ":available": ConnectorStatus.AVAILABLE.value,
                    ":in_use": ConnectorStatus.IN_USE.value,
                    ":job_id": job.job_id,
                    ":now": self._clock.now(),
                    ":one": 1,
                },
            )
        )

    def _new_item(self, record: StartJobRecord) -> JobItem:
        job = record.job
        now = self._clock.now()
        return JobItem(
            tenant_arn_prefix=job.connector.tenant.arn_prefix,
            job_id=job.job_id,
            connector_key=job.connector.arn,
            destination_id=job.connector.destination_id,
            connector_id=job.connector.connector_id,
            arn=job.arn,
            status=JobStatus.STARTED,
            created_at=float(now),
            updated_at=float(now),
            ttl=int(now) + self._settings.active_record_retention_seconds,
            encrypted_configuration=record.encrypted_configuration,
        )


def _key(job: JobRef) -> ItemKey:
    return ItemKey(partition=job.connector.tenant.arn_prefix, sort=job.job_id)


def _connector_key(connector: ConnectorRef) -> ItemKey:
    return ItemKey(partition=connector.tenant.arn_prefix, sort=connector.destination_connector_key)


def _owner(job: JobRef) -> dict[str, str]:
    return {":destination_id": job.connector.destination_id, ":connector_id": job.connector.connector_id}
