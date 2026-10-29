"""Tenant- and connector-scoped DynamoDB lifecycle for connector jobs."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from time import time
from typing import TYPE_CHECKING, Any

from boto3.dynamodb.conditions import Attr, Key
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError
from ccf_api_models.models import ConnectorStatus, JobStatus, JobSummary, ServiceExecution

from common.storage.ddb.custom_connectors_dao import (
    CustomConnectorsDao,
    GetConnectorRequest,
    UpdateConnectorStatusRequest,
)
from common.storage.errors import StorageConflictError, StorageNotFoundError, StorageOperationError
from common.tenant import TenantContext

if TYPE_CHECKING:
    from mypy_boto3_dynamodb import DynamoDBClient
    from mypy_boto3_dynamodb.service_resource import Table

_TERMINAL_STATUSES = frozenset({JobStatus.STOPPED, JobStatus.FAILED, JobStatus.SUCCEEDED})
_VALID_TRANSITIONS = {
    JobStatus.STARTED: frozenset({JobStatus.RUNNING, JobStatus.STOPPING, JobStatus.FAILED, JobStatus.SUCCEEDED}),
    JobStatus.RUNNING: frozenset({JobStatus.STOPPING, JobStatus.FAILED, JobStatus.SUCCEEDED}),
    JobStatus.STOPPING: frozenset({JobStatus.STOPPED, JobStatus.FAILED, JobStatus.SUCCEEDED}),
}
_TTL_SECONDS = 7 * 24 * 60 * 60
_CONDITIONAL_FAILURE_CODES = frozenset({"ConditionalCheckFailed", "ConditionalCheckFailedException"})


class BatchTerminalStatus(str, Enum):
    """Actionable terminal statuses emitted by AWS Batch."""

    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class ClaimStartOrchestrationRequest:
    """Tenant-scoped job identity for one internal start-orchestration claim."""

    tenant_context: TenantContext
    connector_id: str
    job_id: str


@dataclass(frozen=True)
class StartFailureCompensationRequest:
    """Persisted start-failure compensation state for safe stream replay."""

    tenant_context: TenantContext
    connector_id: str
    job_id: str
    batch_job_id: str | None = None
    job_definition_arn: str | None = None


@dataclass(frozen=True)
class StartJobRequest:
    """Service-owned identity and encrypted immutable job configuration."""

    tenant_context: TenantContext
    connector_id: str
    encrypted_configuration: bytes
    job_id: str | None = None
    idempotency_key: str | None = None


@dataclass(frozen=True)
class BatchJobDefinitionRequest:
    """Exact private Batch job-definition identity for one connector job."""

    tenant_context: TenantContext
    connector_id: str
    job_id: str
    job_definition_arn: str


@dataclass(frozen=True)
class BatchSubmissionIdentityRequest:
    """Deterministic Batch submission identity persisted before SubmitJob."""

    tenant_context: TenantContext
    connector_id: str
    job_id: str
    job_queue: str
    job_name: str


@dataclass(frozen=True)
class UpdateJobStatusRequest:
    """Service-owned job status transition."""

    tenant_context: TenantContext
    connector_id: str
    job_id: str
    status: JobStatus
    batch_job_id: str | None = None
    service_execution: ServiceExecution | None = None


@dataclass(frozen=True)
class ReconcileBatchJobStatusRequest:
    """Validated identity and terminal state from one AWS Batch event."""

    tenant_context: TenantContext
    connector_id: str
    job_id: str
    batch_job_id: str
    batch_status: BatchTerminalStatus


@dataclass(frozen=True)
class JobStatusTransition:
    """One validated optimistic job status write."""

    request: UpdateJobStatusRequest
    current_status: JobStatus
    current_version: int
    updated_at: float
    current_service_execution: ServiceExecution | None


@dataclass(frozen=True)
class GetJobRequest:
    """Tenant- and connector-scoped job lookup."""

    tenant_context: TenantContext
    connector_id: str
    job_id: str


@dataclass(frozen=True)
class ListJobsRequest:
    """Tenant- and connector-scoped job listing."""

    tenant_context: TenantContext
    connector_id: str
    max_results: int = 50
    next_token: str | None = None
    status: JobStatus | None = None


@dataclass(frozen=True)
class ListJobsResponse:
    """Generated job summaries and opaque pagination state."""

    jobs: list[JobSummary]
    next_token: str | None = None


@dataclass(frozen=True)
class StoredJob:
    """Job record whose immutable configuration remains encrypted."""

    job_id: str
    connector_id: str
    status: JobStatus
    encrypted_configuration: bytes
    created_at: float
    updated_at: float
    version: int = 1
    batch_job_id: str | None = None
    batch_job_definition_arn: str | None = None
    batch_job_queue: str | None = None
    batch_job_name: str | None = None
    start_orchestration_claimed_at: float | None = None
    start_failure_compensation_started_at: float | None = None
    service_execution: ServiceExecution | None = None


class CustomConnectorJobsDao:
    """Manages connector job creation, lookup, listing, and status transitions."""

    def __init__(
        self,
        jobs_table: Table,
        connectors_dao: CustomConnectorsDao,
        transaction_client: DynamoDBClient,
    ) -> None:
        self.table = jobs_table
        self.connectors_dao = connectors_dao
        self._transaction_client = transaction_client

    def start_job(self, request: StartJobRequest) -> JobSummary:
        """Create a STARTED job and reserve its connector in one transaction."""
        job_id = request.job_id or f"ccj-{uuid.uuid4().hex[:12]}"
        now = time()
        item = {
            "custom_connector_arn_prefix": request.tenant_context.get_arn_prefix(),
            "job_id": job_id,
            "connector_id": request.connector_id,
            "status": JobStatus.STARTED.value,
            "encrypted_configuration": request.encrypted_configuration,
            "created_at": Decimal(str(now)),
            "updated_at": Decimal(str(now)),
            "version": 1,
        }
        try:
            self._write_start_transaction(request, item, now)
        except ClientError as error:
            is_conditional_conflict = self._is_conditional_conflict(error)
            is_transaction_contention = self._is_transaction_contention(error)
            if request.idempotency_key is not None and (is_conditional_conflict or is_transaction_contention):
                existing_item = self._get_existing_idempotent_job(request, job_id)
                if existing_item is not None:
                    return self._summary(existing_item)
            if is_transaction_contention:
                raise StorageOperationError("Job creation failed") from error
            if is_conditional_conflict:
                raise StorageConflictError("Connector reservation or job identity changed") from error
            raise StorageOperationError("Job creation failed") from error
        return self._summary(item)

    def _get_existing_idempotent_job(
        self,
        request: StartJobRequest,
        job_id: str,
    ) -> dict[str, Any] | None:
        try:
            response = self.table.get_item(
                Key=self._key(request.tenant_context, job_id),
                ConsistentRead=True,
            )
        except ClientError as error:
            raise StorageOperationError("Job lookup failed") from error
        existing_item = response.get("Item")
        if not isinstance(existing_item, dict):
            return None
        if existing_item.get("connector_id") != request.connector_id:
            return None
        return existing_item

    def claim_start_orchestration(self, request: ClaimStartOrchestrationRequest) -> bool:
        """Claim one STARTED job before performing an external Batch side effect."""
        self._verify_connector(request.tenant_context, request.connector_id)
        claimed_at = time()
        try:
            self.table.update_item(
                Key=self._key(request.tenant_context, request.job_id),
                UpdateExpression=(
                    "SET start_orchestration_claimed_at = :claimed_at, updated_at = :claimed_at ADD #version :one"
                ),
                ConditionExpression=(
                    "connector_id = :connector_id AND #status = :started "
                    "AND attribute_not_exists(start_orchestration_claimed_at)"
                ),
                ExpressionAttributeNames={"#status": "status", "#version": "version"},
                ExpressionAttributeValues={
                    ":connector_id": request.connector_id,
                    ":started": JobStatus.STARTED.value,
                    ":claimed_at": Decimal(str(claimed_at)),
                    ":one": 1,
                },
            )
        except ClientError as error:
            if self._error_code(error) != "ConditionalCheckFailedException":
                raise StorageOperationError("Job start-orchestration claim failed") from error
            item = self._get_item(request.tenant_context, request.job_id)
            if item.get("connector_id") != request.connector_id:
                raise StorageNotFoundError("Job does not exist") from error
            if JobStatus(str(item["status"])) is not JobStatus.STARTED:
                return False
            if "start_orchestration_claimed_at" in item:
                return False
            raise StorageConflictError("Job changed during start-orchestration claim") from error
        return True

    def mark_start_failure_compensation(
        self,
        request: StartFailureCompensationRequest,
    ) -> None:
        """Persist failure compensation before attempting any compensating side effect."""
        compensation_started_at = time()
        updates = [
            "start_failure_compensation_started_at = :compensation_started_at",
            "updated_at = :compensation_started_at",
        ]
        conditions = [
            "connector_id = :connector_id",
            "#status = :started",
            "attribute_exists(start_orchestration_claimed_at)",
        ]
        values: dict[str, Any] = {
            ":connector_id": request.connector_id,
            ":started": JobStatus.STARTED.value,
            ":compensation_started_at": Decimal(str(compensation_started_at)),
            ":one": 1,
        }
        if request.batch_job_id is not None:
            updates.append("batch_job_id = :batch_job_id")
            values[":batch_job_id"] = request.batch_job_id
        if request.job_definition_arn is not None:
            updates.append("batch_job_definition_arn = :definition_arn")
            conditions.append(
                "(attribute_not_exists(batch_job_definition_arn) OR batch_job_definition_arn = :definition_arn)"
            )
            values[":definition_arn"] = request.job_definition_arn
        try:
            self.table.update_item(
                Key=self._key(request.tenant_context, request.job_id),
                UpdateExpression=f"SET {', '.join(updates)} ADD #version :one",
                ConditionExpression=" AND ".join(conditions),
                ExpressionAttributeNames={"#status": "status", "#version": "version"},
                ExpressionAttributeValues=values,
            )
        except ClientError as error:
            if self._error_code(error) != "ConditionalCheckFailedException":
                raise StorageOperationError("Start failure compensation state update failed") from error
            item = self._get_item(request.tenant_context, request.job_id)
            if item.get("connector_id") != request.connector_id:
                raise StorageNotFoundError("Job does not exist") from error
            if "start_failure_compensation_started_at" in item and (
                request.job_definition_arn is None or item.get("batch_job_definition_arn") == request.job_definition_arn
            ):
                return
            raise StorageConflictError("Job changed before start failure compensation") from error

    def store_batch_job_definition(self, request: BatchJobDefinitionRequest) -> None:
        """Persist the exact per-run definition before submitting the Batch job."""
        updated_at = time()
        try:
            self.table.update_item(
                Key=self._key(request.tenant_context, request.job_id),
                UpdateExpression=(
                    "SET batch_job_definition_arn = :definition_arn, updated_at = :updated_at ADD #version :one"
                ),
                ConditionExpression=(
                    "connector_id = :connector_id AND #status = :started AND "
                    "(attribute_not_exists(batch_job_definition_arn) OR "
                    "batch_job_definition_arn = :definition_arn)"
                ),
                ExpressionAttributeNames={"#status": "status", "#version": "version"},
                ExpressionAttributeValues={
                    ":connector_id": request.connector_id,
                    ":started": JobStatus.STARTED.value,
                    ":definition_arn": request.job_definition_arn,
                    ":updated_at": Decimal(str(updated_at)),
                    ":one": 1,
                },
            )
        except ClientError as error:
            if self._error_code(error) != "ConditionalCheckFailedException":
                raise StorageOperationError("Batch job definition persistence failed") from error
            item = self._get_item(request.tenant_context, request.job_id)
            if item.get("connector_id") != request.connector_id:
                raise StorageNotFoundError("Job does not exist") from error
            if item.get("batch_job_definition_arn") == request.job_definition_arn:
                return
            raise StorageConflictError("Job definition changed during persistence") from error

    def store_batch_submission_identity(self, request: BatchSubmissionIdentityRequest) -> None:
        """Persist the exact queue and job name before SubmitJob is attempted."""
        updated_at = time()
        try:
            self.table.update_item(
                Key=self._key(request.tenant_context, request.job_id),
                UpdateExpression=(
                    "SET batch_job_queue = :job_queue, batch_job_name = :job_name, "
                    "updated_at = :updated_at ADD #version :one"
                ),
                ConditionExpression=(
                    "connector_id = :connector_id AND #status = :started AND "
                    "(attribute_not_exists(batch_job_queue) OR batch_job_queue = :job_queue) AND "
                    "(attribute_not_exists(batch_job_name) OR batch_job_name = :job_name)"
                ),
                ExpressionAttributeNames={"#status": "status", "#version": "version"},
                ExpressionAttributeValues={
                    ":connector_id": request.connector_id,
                    ":started": JobStatus.STARTED.value,
                    ":job_queue": request.job_queue,
                    ":job_name": request.job_name,
                    ":updated_at": Decimal(str(updated_at)),
                    ":one": 1,
                },
            )
        except ClientError as error:
            if self._error_code(error) != "ConditionalCheckFailedException":
                raise StorageOperationError("Batch submission identity persistence failed") from error
            item = self._get_item(request.tenant_context, request.job_id)
            if item.get("connector_id") != request.connector_id:
                raise StorageNotFoundError("Job does not exist") from error
            if item.get("batch_job_queue") == request.job_queue and item.get("batch_job_name") == request.job_name:
                return
            raise StorageConflictError("Batch submission identity changed during persistence") from error

    def clear_batch_job_definition(self, request: BatchJobDefinitionRequest) -> None:
        """Clear only the exact definition ARN after successful deregistration."""
        updated_at = time()
        try:
            self.table.update_item(
                Key=self._key(request.tenant_context, request.job_id),
                UpdateExpression=("SET updated_at = :updated_at REMOVE batch_job_definition_arn ADD #version :one"),
                ConditionExpression=("connector_id = :connector_id AND batch_job_definition_arn = :definition_arn"),
                ExpressionAttributeNames={"#version": "version"},
                ExpressionAttributeValues={
                    ":connector_id": request.connector_id,
                    ":definition_arn": request.job_definition_arn,
                    ":updated_at": Decimal(str(updated_at)),
                    ":one": 1,
                },
            )
        except ClientError as error:
            if self._error_code(error) != "ConditionalCheckFailedException":
                raise StorageOperationError("Batch job definition cleanup state update failed") from error
            item = self._get_item(request.tenant_context, request.job_id)
            if item.get("connector_id") != request.connector_id:
                raise StorageNotFoundError("Job does not exist") from error
            stored_arn = item.get("batch_job_definition_arn")
            if stored_arn is None:
                return
            if stored_arn != request.job_definition_arn:
                raise StorageConflictError("Batch job definition changed during cleanup") from error

    def _write_start_transaction(
        self,
        request: StartJobRequest,
        item: dict[str, Any],
        updated_at: float,
    ) -> None:
        tenant_key = request.tenant_context.get_arn_prefix()
        serializer = TypeSerializer()
        self._transaction_client.transact_write_items(
            TransactItems=[
                {
                    "Update": {
                        "TableName": self.connectors_dao.table.name,
                        "Key": self._serialize(
                            serializer,
                            {
                                "custom_connector_arn_prefix": tenant_key,
                                "connector_id": request.connector_id,
                            },
                        ),
                        "UpdateExpression": (
                            "SET #status = :in_use, active_job_id = :job_id, updated_at = :updated_at ADD #version :one"
                        ),
                        "ConditionExpression": (
                            "attribute_exists(connector_id) AND #status = :available "
                            "AND attribute_not_exists(active_job_id) "
                            "AND attribute_not_exists(deletion_claimed_at)"
                        ),
                        "ExpressionAttributeNames": {"#status": "status", "#version": "version"},
                        "ExpressionAttributeValues": self._serialize(
                            serializer,
                            {
                                ":in_use": ConnectorStatus.IN_USE.value,
                                ":available": ConnectorStatus.AVAILABLE.value,
                                ":job_id": str(item["job_id"]),
                                ":updated_at": Decimal(str(updated_at)),
                                ":one": 1,
                            },
                        ),
                    }
                },
                {
                    "Put": {
                        "TableName": self.table.name,
                        "Item": self._serialize(serializer, item),
                        "ConditionExpression": "attribute_not_exists(job_id)",
                    }
                },
            ]
        )

    def get_job(self, request: GetJobRequest) -> StoredJob:
        """Fetch one job and hide cross-connector lookups as not found."""
        self._verify_connector(request.tenant_context, request.connector_id)
        item = self._get_item(request.tenant_context, request.job_id)
        if item.get("connector_id") != request.connector_id:
            raise StorageNotFoundError("Job does not exist")
        return self._stored_job(item)

    def validate_batch_execution(self, request: ReconcileBatchJobStatusRequest) -> JobStatus:
        """Reject stale Batch events before any destination-side effect."""
        self._verify_connector(request.tenant_context, request.connector_id)
        item = self._get_item(request.tenant_context, request.job_id)
        if item.get("connector_id") != request.connector_id:
            raise StorageNotFoundError("Job does not exist")
        if item.get("batch_job_id") != request.batch_job_id:
            raise StorageConflictError("Batch execution does not match the job")
        return JobStatus(str(item["status"]))

    def is_batch_completion_pending(self, request: ReconcileBatchJobStatusRequest) -> bool:
        """Return whether a matching Batch event still needs a terminal transition."""
        return self.validate_batch_execution(request) not in _TERMINAL_STATUSES

    def reconcile_batch_job_status(self, request: ReconcileBatchJobStatusRequest) -> JobStatus:
        """Map one matching Batch terminal event to a monotonic CCF job status."""
        current_status = self.validate_batch_execution(request)
        if current_status in _TERMINAL_STATUSES:
            self._release_connector(
                request.tenant_context,
                request.connector_id,
                request.job_id,
            )
            return current_status
        target_status = self._batch_target_status(request.batch_status, current_status)
        self.update_job_status(
            UpdateJobStatusRequest(
                tenant_context=request.tenant_context,
                connector_id=request.connector_id,
                job_id=request.job_id,
                status=target_status,
                batch_job_id=request.batch_job_id,
            )
        )
        return target_status

    def update_job_status(self, request: UpdateJobStatusRequest) -> None:
        """Apply one valid status transition with status and version conditions."""
        self._verify_connector(request.tenant_context, request.connector_id)
        item = self._get_item(request.tenant_context, request.job_id)
        if item.get("connector_id") != request.connector_id:
            raise StorageNotFoundError("Job does not exist")
        current_status = JobStatus(str(item["status"]))
        if current_status is request.status:
            self._validate_terminal_execution(item, request)
            self._release_if_terminal(request)
            return
        if request.status not in _VALID_TRANSITIONS.get(current_status, frozenset()):
            raise StorageConflictError("Job status transition is invalid")
        transition = JobStatusTransition(
            request=request,
            current_status=current_status,
            current_version=int(item.get("version", 1)),
            updated_at=time(),
            current_service_execution=(
                ServiceExecution.model_validate(item["service_execution"])
                if item.get("service_execution") is not None
                else None
            ),
        )
        try:
            self._write_status_transition(transition)
        except ClientError as error:
            if self._error_code(error) != "ConditionalCheckFailedException":
                raise StorageOperationError("Job status update failed") from error
            changed_item = self._get_item(request.tenant_context, request.job_id)
            if changed_item.get("connector_id") != request.connector_id:
                raise StorageNotFoundError("Job does not exist") from error
            if JobStatus(str(changed_item["status"])) is request.status:
                self._validate_terminal_execution(changed_item, request)
                self._release_if_terminal(request)
                return
            raise StorageConflictError("Job changed during status transition") from error
        self._release_if_terminal(request)

    def _write_status_transition(self, transition: JobStatusTransition) -> None:
        request = transition.request
        updates = ["#status = :status", "updated_at = :updated_at", "#version = :new_version"]
        conditions = [
            "connector_id = :connector_id",
            "#status = :expected_status",
            "#version = :current_version",
        ]
        names = {"#status": "status", "#version": "version"}
        values: dict[str, Any] = {
            ":status": request.status.value,
            ":expected_status": transition.current_status.value,
            ":current_version": transition.current_version,
            ":new_version": transition.current_version + 1,
            ":updated_at": Decimal(str(transition.updated_at)),
            ":connector_id": request.connector_id,
        }
        if transition.current_status is JobStatus.STARTED and request.status is JobStatus.STOPPING:
            conditions.append("attribute_not_exists(start_orchestration_claimed_at)")
        if request.batch_job_id is not None:
            values[":batch_job_id"] = request.batch_job_id
            if request.status in _TERMINAL_STATUSES:
                if transition.current_status is JobStatus.STARTED:
                    updates.append("batch_job_id = :batch_job_id")
                    conditions.append("(attribute_not_exists(batch_job_id) OR batch_job_id = :batch_job_id)")
                else:
                    conditions.append("batch_job_id = :batch_job_id")
            else:
                updates.append("batch_job_id = :batch_job_id")
        if request.service_execution is not None:
            updates.append("service_execution = :service_execution")
            values[":service_execution"] = self._to_dynamodb(request.service_execution.model_dump(mode="json"))
        if request.status in _TERMINAL_STATUSES:
            updates.append("#ttl = :ttl")
            names["#ttl"] = "ttl"
            values[":ttl"] = int(transition.updated_at) + _TTL_SECONDS
            if transition.current_service_execution is not None:
                completed_execution = transition.current_service_execution.model_copy(
                    update={"completed_at": transition.updated_at}
                )
                updates.append("service_execution = :completed_execution")
                values[":completed_execution"] = self._to_dynamodb(completed_execution.model_dump(mode="json"))
        self.table.update_item(
            Key=self._key(request.tenant_context, request.job_id),
            UpdateExpression=f"SET {', '.join(updates)}",
            ConditionExpression=" AND ".join(conditions),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )

    @staticmethod
    def _validate_terminal_execution(item: dict[str, Any], request: UpdateJobStatusRequest) -> None:
        if (
            request.status in _TERMINAL_STATUSES
            and request.batch_job_id is not None
            and item.get("batch_job_id") != request.batch_job_id
        ):
            raise StorageConflictError("Batch execution does not match the job")

    def _release_if_terminal(self, request: UpdateJobStatusRequest) -> None:
        if request.status in _TERMINAL_STATUSES:
            self._release_connector(request.tenant_context, request.connector_id, request.job_id)

    @staticmethod
    def _batch_target_status(
        batch_status: BatchTerminalStatus,
        current_status: JobStatus,
    ) -> JobStatus:
        if batch_status is BatchTerminalStatus.SUCCEEDED:
            return JobStatus.SUCCEEDED
        if current_status is JobStatus.STOPPING:
            return JobStatus.STOPPED
        return JobStatus.FAILED

    def list_jobs(self, request: ListJobsRequest) -> ListJobsResponse:
        """List only jobs belonging to the requested tenant and connector."""
        self._verify_connector(request.tenant_context, request.connector_id)
        query: dict[str, Any] = {
            "IndexName": "GSI1",
            "KeyConditionExpression": Key("custom_connector_arn_prefix").eq(request.tenant_context.get_arn_prefix())
            & Key("connector_id").eq(request.connector_id),
            "Limit": request.max_results,
        }
        if request.status is not None:
            query["FilterExpression"] = Attr("status").eq(request.status.value)
        if request.next_token is not None:
            query["ExclusiveStartKey"] = self._decode_token(request.next_token)
        try:
            response = self.table.query(**query)
        except ClientError as error:
            raise StorageOperationError("Job listing failed") from error
        return ListJobsResponse(
            jobs=[self._summary(item) for item in response.get("Items", [])],
            next_token=self._encode_token(response.get("LastEvaluatedKey")),
        )

    def _verify_connector(self, tenant: TenantContext, connector_id: str) -> None:
        self.connectors_dao.get_connector(GetConnectorRequest(tenant, connector_id))

    def _get_item(self, tenant: TenantContext, job_id: str) -> dict[str, Any]:
        try:
            response = self.table.get_item(Key=self._key(tenant, job_id))
        except ClientError as error:
            raise StorageOperationError("Job lookup failed") from error
        item = response.get("Item")
        if not isinstance(item, dict):
            raise StorageNotFoundError("Job does not exist")
        return item

    def _release_connector(self, tenant: TenantContext, connector_id: str, job_id: str) -> None:
        try:
            self.connectors_dao.update_connector_status(
                UpdateConnectorStatusRequest(tenant, connector_id, ConnectorStatus.AVAILABLE, job_id)
            )
        except (StorageConflictError, StorageNotFoundError):
            return

    @staticmethod
    def _serialize(serializer: TypeSerializer, values: dict[str, Any]) -> dict[str, Any]:
        return {name: serializer.serialize(value) for name, value in values.items()}

    @staticmethod
    def _to_dynamodb(value: Any) -> Any:
        if isinstance(value, float):
            return Decimal(str(value))
        if isinstance(value, dict):
            return {key: CustomConnectorJobsDao._to_dynamodb(nested) for key, nested in value.items()}
        if isinstance(value, list):
            return [CustomConnectorJobsDao._to_dynamodb(nested) for nested in value]
        return value

    @staticmethod
    def _key(tenant: TenantContext, job_id: str) -> dict[str, str]:
        return {"custom_connector_arn_prefix": tenant.get_arn_prefix(), "job_id": job_id}

    @staticmethod
    def _summary(item: dict[str, Any]) -> JobSummary:
        return JobSummary(
            job_id=str(item["job_id"]),
            connector_id=str(item["connector_id"]),
            status=JobStatus(item["status"]),
            created_at=float(item["created_at"]),
            updated_at=float(item["updated_at"]),
        )

    @staticmethod
    def _stored_job(item: dict[str, Any]) -> StoredJob:
        encrypted = item["encrypted_configuration"]
        ciphertext = encrypted.value if hasattr(encrypted, "value") else bytes(encrypted)
        return StoredJob(
            job_id=str(item["job_id"]),
            connector_id=str(item["connector_id"]),
            status=JobStatus(item["status"]),
            encrypted_configuration=ciphertext,
            created_at=float(item["created_at"]),
            updated_at=float(item["updated_at"]),
            version=int(item.get("version", 1)),
            batch_job_id=item.get("batch_job_id"),
            batch_job_definition_arn=item.get("batch_job_definition_arn"),
            batch_job_queue=item.get("batch_job_queue"),
            batch_job_name=item.get("batch_job_name"),
            start_orchestration_claimed_at=(
                float(item["start_orchestration_claimed_at"])
                if item.get("start_orchestration_claimed_at") is not None
                else None
            ),
            start_failure_compensation_started_at=(
                float(item["start_failure_compensation_started_at"])
                if item.get("start_failure_compensation_started_at") is not None
                else None
            ),
            service_execution=(
                ServiceExecution.model_validate(item["service_execution"])
                if item.get("service_execution") is not None
                else None
            ),
        )

    @staticmethod
    def _decode_token(token: str) -> dict[str, Any]:
        try:
            decoded: Any = json.loads(token)
        except json.JSONDecodeError as error:
            raise StorageOperationError("Pagination token is invalid") from error
        if not isinstance(decoded, dict):
            raise StorageOperationError("Pagination token is invalid")
        return decoded

    @staticmethod
    def _encode_token(key: Any) -> str | None:
        return json.dumps(key) if isinstance(key, dict) else None

    @classmethod
    def _is_conditional_conflict(cls, error: ClientError) -> bool:
        error_code = cls._error_code(error)
        if error_code == "ConditionalCheckFailedException":
            return True
        if error_code != "TransactionCanceledException":
            return False
        cancellation_reasons = error.response.get("CancellationReasons", [])
        return any(
            isinstance(reason, dict) and reason.get("Code") in _CONDITIONAL_FAILURE_CODES
            for reason in cancellation_reasons
        )

    @classmethod
    def _is_transaction_contention(cls, error: ClientError) -> bool:
        if cls._error_code(error) != "TransactionCanceledException":
            return False
        cancellation_reasons = error.response.get("CancellationReasons", [])
        return any(
            isinstance(reason, dict) and reason.get("Code") == "TransactionConflict" for reason in cancellation_reasons
        )

    @staticmethod
    def _error_code(error: ClientError) -> str:
        return str(error.response.get("Error", {}).get("Code", ""))
