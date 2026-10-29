"""Retry-safe cleanup for private per-run AWS Batch job definitions."""

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, TypeGuard

from common.storage.ddb.custom_connector_jobs_dao import (
    BatchJobDefinitionRequest,
    CustomConnectorJobsDao,
    GetJobRequest,
)
from common.tenant import TenantContext

if TYPE_CHECKING:
    from mypy_boto3_batch import BatchClient


class JobDefinitionStatus(str, Enum):
    """AWS Batch job-definition lifecycle states relevant to cleanup."""

    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


def _is_exact_job_definition_arn(
    candidate: object,
    job_id: str,
) -> TypeGuard[str]:
    """Return whether a value is the exact revised job-definition ARN."""
    if not isinstance(candidate, str):
        return False
    try:
        arn_prefix, partition, service, region, account, resource = candidate.split(":", 5)
    except ValueError:
        return False
    resource_type, resource_separator, name_and_revision = resource.partition("/")
    job_name, revision_separator, revision = name_and_revision.rpartition(":")
    return bool(
        arn_prefix == "arn"
        and partition
        and service == "batch"
        and region
        and account
        and resource_type == "job-definition"
        and resource_separator
        and job_name == job_id
        and revision_separator
        and revision
        and revision.isascii()
        and revision.isdecimal()
    )


@dataclass(frozen=True)
class CleanupBatchJobDefinitionRequest:
    """Job identity and optional known definition ARN."""

    tenant_context: TenantContext
    connector_id: str
    job_id: str
    job_definition_arn: str | None = None


class BatchJobDefinitionCleanup:
    """Deregister persisted or exact-name-discovered per-run definitions."""

    def __init__(self, batch_client: BatchClient, jobs_dao: CustomConnectorJobsDao) -> None:
        self._batch_client = batch_client
        self._jobs_dao = jobs_dao

    def cleanup(self, request: CleanupBatchJobDefinitionRequest) -> None:
        """Deregister persisted or discoverable per-run definitions retry-safely."""
        stored_job = self._jobs_dao.get_job(
            GetJobRequest(
                request.tenant_context,
                request.connector_id,
                request.job_id,
            )
        )
        job_definition_arn = stored_job.batch_job_definition_arn
        if job_definition_arn is None:
            self._cleanup_active_revisions_by_name(request)
            return

        described_definitions = self._batch_client.describe_job_definitions(jobDefinitions=[job_definition_arn])[
            "jobDefinitions"
        ]
        exact_definition = next(
            (
                definition
                for definition in described_definitions
                if definition.get("jobDefinitionArn") == job_definition_arn
            ),
            None,
        )
        if exact_definition is not None:
            status = JobDefinitionStatus(exact_definition.get("status"))
            if status is JobDefinitionStatus.ACTIVE:
                self._batch_client.deregister_job_definition(jobDefinition=job_definition_arn)
        self._jobs_dao.clear_batch_job_definition(
            BatchJobDefinitionRequest(
                request.tenant_context,
                request.connector_id,
                request.job_id,
                job_definition_arn,
            )
        )

    def _cleanup_active_revisions_by_name(self, request: CleanupBatchJobDefinitionRequest) -> None:
        """Deregister every exact active revision found for the per-run name."""
        next_token: str | None = None
        while True:
            describe_request: dict[str, Any] = {
                "jobDefinitionName": request.job_id,
                "status": JobDefinitionStatus.ACTIVE.value,
            }
            if next_token:
                describe_request["nextToken"] = next_token
            response = self._batch_client.describe_job_definitions(**describe_request)
            for definition in response["jobDefinitions"]:
                if definition.get("jobDefinitionName") != request.job_id:
                    continue
                discovered_arn = definition.get("jobDefinitionArn")
                if not _is_exact_job_definition_arn(
                    discovered_arn,
                    request.job_id,
                ):
                    continue
                self._batch_client.deregister_job_definition(jobDefinition=discovered_arn)
            next_token = response.get("nextToken")
            if not next_token:
                return
