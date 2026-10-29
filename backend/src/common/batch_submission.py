"""Recovery for a Batch submission whose SubmitJob response was lost.

CCF persists a deterministic queue, job name, and job-definition revision before
it calls SubmitJob. When the response is lost, replay proves ownership of at
most one execution from that persisted identity rather than from an ambiguous
name prefix or a shared resource tag. Zero, multiple, or unverifiable matches
fail closed so the connector reservation is never released while a container
may still be running.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mypy_boto3_batch import BatchClient
    from mypy_boto3_batch.type_defs import JobSummaryTypeDef

_JOB_NAME_FILTER = "JOB_NAME"


class BatchSubmissionRecoveryError(RuntimeError):
    """Raised when a possibly submitted Batch execution cannot be proven unique."""


@dataclass(frozen=True)
class SubmittedExecutionIdentity:
    """The exact persisted identity of one possibly submitted Batch execution."""

    job_queue: str
    job_name: str
    job_definition_arn: str


class SubmittedExecutionLocator:
    """Finds the single Batch execution owned by one persisted submission identity."""

    def __init__(self, batch_client: BatchClient) -> None:
        self._batch_client = batch_client

    def locate(self, identity: SubmittedExecutionIdentity) -> str:
        """Return the job id of the one owned execution, or fail closed."""
        try:
            owned_job_ids = self._owned_job_ids(identity)
        except Exception as error:
            raise BatchSubmissionRecoveryError("Batch submission ownership could not be verified") from error
        if len(owned_job_ids) != 1:
            raise BatchSubmissionRecoveryError("Batch submission ownership did not resolve to exactly one execution")
        return owned_job_ids[0]

    def _owned_job_ids(self, identity: SubmittedExecutionIdentity) -> list[str]:
        owned_job_ids: list[str] = []
        next_token: str | None = None
        while True:
            arguments: dict[str, Any] = {
                "jobQueue": identity.job_queue,
                "filters": [{"name": _JOB_NAME_FILTER, "values": [identity.job_name]}],
            }
            if next_token:
                arguments["nextToken"] = next_token
            response = self._batch_client.list_jobs(**arguments)
            owned_job_ids.extend(
                summary["jobId"] for summary in response["jobSummaryList"] if self._is_owned(summary, identity)
            )
            next_token = response.get("nextToken")
            if not next_token:
                return owned_job_ids

    @staticmethod
    def _is_owned(summary: JobSummaryTypeDef, identity: SubmittedExecutionIdentity) -> bool:
        return (
            summary.get("jobName") == identity.job_name
            and summary.get("jobDefinition") == identity.job_definition_arn
            and bool(summary.get("jobId"))
        )
