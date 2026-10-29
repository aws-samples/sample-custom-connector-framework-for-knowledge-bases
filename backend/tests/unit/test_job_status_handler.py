"""Batch terminal-event identity and transition tests."""

from unittest.mock import MagicMock, patch

import pytest
from ccf_api_models.models import JobStatus

import job_status_handler
from common.storage.ddb.custom_connector_jobs_dao import BatchTerminalStatus


def batch_event(status: str = "SUCCEEDED", batch_job_id: str | None = "batch-1") -> dict:
    event = {
        "jobName": "job-1",
        "status": status,
        "container": {
            "environment": [
                {"name": "CUSTOM_CONNECTOR_FRAMEWORK_CUSTOM_CONNECTOR_ID", "value": "connector-1"},
                {
                    "name": "CUSTOM_CONNECTOR_FRAMEWORK_CUSTOM_CONNECTOR_ARN_PREFIX",
                    "value": "arn:aws:ccf:us-west-2:123456789012",
                },
                {"name": "UNRELATED", "value": "ignored"},
            ]
        },
    }
    if batch_job_id is not None:
        event["jobId"] = batch_job_id
    return event


@pytest.mark.parametrize(
    ("batch_status", "expected_batch_status", "returned_status"),
    [
        ("SUCCEEDED", BatchTerminalStatus.SUCCEEDED, JobStatus.SUCCEEDED),
        ("FAILED", BatchTerminalStatus.FAILED, JobStatus.FAILED),
    ],
)
def test_process_batch_event_passes_exact_execution_identity_to_completion_activity(
    batch_status: str,
    expected_batch_status: BatchTerminalStatus,
    returned_status: JobStatus,
) -> None:
    activity = MagicMock()
    activity.complete.return_value = returned_status

    with patch.object(job_status_handler, "complete_job_activity", activity):
        job_status_handler.process_batch_event(batch_event(batch_status))

    request = activity.complete.call_args.args[0]
    assert request.tenant_context.account_id == "123456789012"
    assert request.connector_id == "connector-1"
    assert request.job_id == "job-1"
    assert request.batch_job_id == "batch-1"
    assert request.batch_status is expected_batch_status


@pytest.mark.parametrize(
    "event_detail",
    [
        batch_event("RUNNING"),
        batch_event(batch_job_id=None),
        {"jobName": "job-1", "jobId": "batch-1", "status": "SUCCEEDED", "container": {}},
    ],
)
def test_process_batch_event_ignores_nonterminal_or_incomplete_events(event_detail: dict) -> None:
    activity = MagicMock()

    with patch.object(job_status_handler, "complete_job_activity", activity):
        job_status_handler.process_batch_event(event_detail)

    activity.complete.assert_not_called()


def test_process_batch_event_propagates_completion_failure_for_eventbridge_retry() -> None:
    activity = MagicMock()
    activity.complete.side_effect = RuntimeError("ingestion failed")

    with (
        patch.object(job_status_handler, "complete_job_activity", activity),
        pytest.raises(RuntimeError, match="ingestion failed"),
    ):
        job_status_handler.process_batch_event(batch_event())


def test_handler_returns_success_after_reconciliation() -> None:
    event = {"detail": batch_event()}
    context = MagicMock(
        function_name="test-function",
        memory_limit_in_mb=128,
        invoked_function_arn="arn:aws:lambda:us-west-2:123456789012:function:test-function",
        aws_request_id="test-request-id",
    )

    with patch("job_status_handler.process_batch_event") as process_batch_event:
        response = job_status_handler.handler(event, context)

    process_batch_event.assert_called_once_with(event["detail"])
    assert response["statusCode"] == 200
