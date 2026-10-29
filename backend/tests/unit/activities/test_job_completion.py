"""Destination completion ordering and retry tests."""

from unittest.mock import MagicMock

import pytest
from ccf_api_models.models import DestinationMode, JobStatus

from activities.job_completion import CompleteBatchJobActivity
from common.storage.ddb.custom_connector_jobs_dao import BatchTerminalStatus, ReconcileBatchJobStatusRequest
from common.tenant import TenantContext

_TENANT = TenantContext("123456789012", "us-east-1")
_REQUEST = ReconcileBatchJobStatusRequest(
    tenant_context=_TENANT,
    connector_id="connector-1",
    job_id="job-1",
    batch_job_id="batch-1",
    batch_status=BatchTerminalStatus.SUCCEEDED,
)


def create_activity(
    destination_mode: DestinationMode,
    jobs_dao: MagicMock,
    job_definition_cleanup: MagicMock,
    bedrock_client: MagicMock,
) -> CompleteBatchJobActivity:
    return CompleteBatchJobActivity(
        jobs_dao=jobs_dao,
        job_definition_cleanup=job_definition_cleanup,
        bedrock_agent_client=bedrock_client,
        destination_mode=destination_mode,
        knowledge_base_id="ABCDEFGHIJ",
        data_source_id="1234567890",
    )


def test_bedrock_ingestion_starts_before_ccf_succeeds_with_stable_token() -> None:
    events: list[str] = []
    jobs_dao = MagicMock()

    pending_results = iter((True, False))

    def completion_pending(request: ReconcileBatchJobStatusRequest) -> bool:
        assert request == _REQUEST
        events.append("pending")
        return next(pending_results)

    def reconcile(request: ReconcileBatchJobStatusRequest) -> JobStatus:
        assert request == _REQUEST
        events.append("reconcile")
        return JobStatus.SUCCEEDED

    def ingest(**kwargs: str) -> None:
        assert kwargs["knowledgeBaseId"] == "ABCDEFGHIJ"
        assert kwargs["dataSourceId"] == "1234567890"
        events.append(f"ingest:{kwargs['clientToken']}")

    jobs_dao.is_batch_completion_pending.side_effect = completion_pending
    jobs_dao.reconcile_batch_job_status.side_effect = reconcile
    bedrock_client = MagicMock()
    bedrock_client.start_ingestion_job.side_effect = ingest
    job_definition_cleanup = MagicMock()
    job_definition_cleanup.cleanup.side_effect = lambda _request: events.append("cleanup")
    activity = create_activity(
        DestinationMode.BEDROCK,
        jobs_dao,
        job_definition_cleanup,
        bedrock_client,
    )

    first_status = activity.complete(_REQUEST)
    second_status = activity.complete(_REQUEST)

    assert first_status is JobStatus.SUCCEEDED
    assert second_status is JobStatus.SUCCEEDED
    first_token = events[1].removeprefix("ingest:")
    assert len(first_token) == 64
    assert events == [
        "pending",
        f"ingest:{first_token}",
        "reconcile",
        "cleanup",
        "pending",
        "reconcile",
        "cleanup",
    ]
    bedrock_client.start_ingestion_job.assert_called_once()


def test_bedrock_ingestion_failure_leaves_job_nonterminal_for_retry() -> None:
    jobs_dao = MagicMock()
    jobs_dao.is_batch_completion_pending.return_value = True
    bedrock_client = MagicMock()
    bedrock_client.start_ingestion_job.side_effect = RuntimeError("ingestion unavailable")
    activity = create_activity(DestinationMode.BEDROCK, jobs_dao, MagicMock(), bedrock_client)

    with pytest.raises(RuntimeError, match="ingestion unavailable"):
        activity.complete(_REQUEST)

    jobs_dao.reconcile_batch_job_status.assert_not_called()


@pytest.mark.parametrize("destination_mode", [DestinationMode.S3, DestinationMode.QUICK])
def test_non_bedrock_destinations_transition_directly(destination_mode: DestinationMode) -> None:
    jobs_dao = MagicMock()
    jobs_dao.is_batch_completion_pending.return_value = True
    jobs_dao.reconcile_batch_job_status.return_value = JobStatus.SUCCEEDED
    bedrock_client = MagicMock()
    activity = create_activity(destination_mode, jobs_dao, MagicMock(), bedrock_client)

    assert activity.complete(_REQUEST) is JobStatus.SUCCEEDED

    bedrock_client.start_ingestion_job.assert_not_called()
    jobs_dao.reconcile_batch_job_status.assert_called_once_with(_REQUEST)


def test_failed_batch_for_stopping_job_reconciles_to_stopped_before_exact_cleanup() -> None:
    events: list[str] = []
    jobs_dao = MagicMock()
    jobs_dao.is_batch_completion_pending.return_value = True
    jobs_dao.reconcile_batch_job_status.side_effect = lambda _request: events.append("reconcile") or JobStatus.STOPPED
    bedrock_client = MagicMock()
    job_definition_cleanup = MagicMock()
    job_definition_cleanup.cleanup.side_effect = lambda _request: events.append("cleanup")
    activity = create_activity(
        DestinationMode.BEDROCK,
        jobs_dao,
        job_definition_cleanup,
        bedrock_client,
    )
    request = ReconcileBatchJobStatusRequest(
        tenant_context=_TENANT,
        connector_id="connector-1",
        job_id="job-1",
        batch_job_id="batch-1",
        batch_status=BatchTerminalStatus.FAILED,
    )

    assert activity.complete(request) is JobStatus.STOPPED

    assert events == ["reconcile", "cleanup"]
    cleanup_request = job_definition_cleanup.cleanup.call_args.args[0]
    assert cleanup_request.tenant_context == _TENANT
    assert cleanup_request.connector_id == "connector-1"
    assert cleanup_request.job_id == "job-1"
    assert cleanup_request.job_definition_arn is None
    bedrock_client.start_ingestion_job.assert_not_called()


def test_definition_cleanup_failure_propagates_after_terminal_reconciliation() -> None:
    jobs_dao = MagicMock()
    jobs_dao.is_batch_completion_pending.return_value = False
    jobs_dao.reconcile_batch_job_status.return_value = JobStatus.SUCCEEDED
    cleanup = MagicMock()
    cleanup.cleanup.side_effect = RuntimeError("cleanup failed")
    activity = create_activity(DestinationMode.BEDROCK, jobs_dao, cleanup, MagicMock())

    with pytest.raises(RuntimeError, match="cleanup failed"):
        activity.complete(_REQUEST)

    jobs_dao.reconcile_batch_job_status.assert_called_once_with(_REQUEST)
    cleanup.cleanup.assert_called_once()


def test_terminal_reconciliation_failure_retains_definition_without_cleanup() -> None:
    reconciliation_error = RuntimeError("terminal persistence failed")
    jobs_dao = MagicMock()
    jobs_dao.is_batch_completion_pending.return_value = False
    jobs_dao.reconcile_batch_job_status.side_effect = reconciliation_error
    cleanup = MagicMock()
    activity = create_activity(DestinationMode.S3, jobs_dao, cleanup, MagicMock())

    with pytest.raises(RuntimeError) as raised_error:
        activity.complete(_REQUEST)

    assert raised_error.value is reconciliation_error
    cleanup.cleanup.assert_not_called()
