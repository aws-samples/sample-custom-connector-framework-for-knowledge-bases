"""Shared scheduled-start handler tests."""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from ccf_api_models.models import JobStatus, JobSummary, StartCustomConnectorJobResponseContent
from pydantic import ValidationError

import scheduled_start_handler

_VALID_EVENT: dict[str, Any] = {
    "custom_connector_arn_prefix": "arn:aws:ccf:us-east-1:123456789012",
    "connector_id": "connector-1",
    "scheduled_time": "2026-10-10T03:04:05Z",
}


def test_handler_reuses_start_activity_with_empty_modeled_job_configuration() -> None:
    response = StartCustomConnectorJobResponseContent(
        job=JobSummary(
            job_id="job-1",
            connector_id="connector-1",
            status=JobStatus.STARTED,
            created_at=1.0,
            updated_at=1.0,
        )
    )
    activity = MagicMock()
    activity.start.return_value = response
    with patch.object(scheduled_start_handler, "start_job_activity", activity):
        result = scheduled_start_handler.handler(_VALID_EVENT, MagicMock())

    request = activity.start.call_args.args[0]
    assert request.tenant.account_id == "123456789012"
    assert request.payload.connector_id == "connector-1"
    assert request.payload.job_configuration.get_secret_value() == "{}"
    assert request.idempotency_key == "2026-10-10T03:04:05.000000+00:00"
    assert result["job"]["job_id"] == "job-1"


@pytest.mark.parametrize(
    "invalid_event",
    [
        {**_VALID_EVENT, "connector_id": "invalid connector"},
        {**_VALID_EVENT, "unexpected": "field"},
        {key: value for key, value in _VALID_EVENT.items() if key != "scheduled_time"},
        {**_VALID_EVENT, "scheduled_time": "2026-10-10T03:04:05"},
        {**_VALID_EVENT, "scheduled_time": "not-a-timestamp"},
    ],
    ids=[
        "malformed-connector-id",
        "unexpected-field",
        "missing-scheduled-time",
        "naive-scheduled-time",
        "malformed-scheduled-time",
    ],
)
def test_handler_rejects_invalid_event_before_start_activity(
    invalid_event: dict[str, Any],
) -> None:
    activity = MagicMock()

    with (
        patch.object(scheduled_start_handler, "start_job_activity", activity),
        pytest.raises(ValidationError),
    ):
        scheduled_start_handler.handler(invalid_event, MagicMock())

    activity.start.assert_not_called()


def test_handler_propagates_start_activity_failure() -> None:
    activity = MagicMock()
    expected_error = RuntimeError("start failed")
    activity.start.side_effect = expected_error

    with (
        patch.object(scheduled_start_handler, "start_job_activity", activity),
        pytest.raises(RuntimeError) as raised_error,
    ):
        scheduled_start_handler.handler(_VALID_EVENT, MagicMock())

    assert raised_error.value is expected_error


def test_handler_normalizes_equivalent_offset_timestamp_to_utc() -> None:
    response = StartCustomConnectorJobResponseContent(
        job=JobSummary(
            job_id="job-1",
            connector_id="connector-1",
            status=JobStatus.STARTED,
            created_at=1.0,
            updated_at=1.0,
        )
    )
    activity = MagicMock()
    activity.start.return_value = response
    offset_event = {
        **_VALID_EVENT,
        "scheduled_time": "2026-10-09T20:04:05-07:00",
    }

    with patch.object(scheduled_start_handler, "start_job_activity", activity):
        scheduled_start_handler.handler(offset_event, MagicMock())

    request = activity.start.call_args.args[0]
    assert request.idempotency_key == "2026-10-10T03:04:05.000000+00:00"
