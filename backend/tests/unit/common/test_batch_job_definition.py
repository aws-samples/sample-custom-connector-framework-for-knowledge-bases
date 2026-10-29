"""Retry-safe per-run Batch job-definition cleanup tests."""

from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest

from common.batch_job_definition import (
    BatchJobDefinitionCleanup,
    CleanupBatchJobDefinitionRequest,
    _is_exact_job_definition_arn,
)
from common.tenant import TenantContext

_TENANT = TenantContext("123456789012", "us-east-1")
_BATCH_ACCOUNT_ID = "210987654321"
_REQUEST = CleanupBatchJobDefinitionRequest(_TENANT, "connector-1", "job-1")
_ACTIVE_DEFINITION = {
    "jobDefinitions": [
        {"jobDefinitionArn": "definition-arn", "status": "ACTIVE"},
    ]
}
_INACTIVE_DEFINITION = {
    "jobDefinitions": [
        {"jobDefinitionArn": "definition-arn", "status": "INACTIVE"},
    ]
}
_FALLBACK_ARN_1 = f"arn:aws:batch:us-east-1:{_BATCH_ACCOUNT_ID}:job-definition/job-1:1"
_FALLBACK_ARN_2 = f"arn:aws:batch:us-east-1:{_BATCH_ACCOUNT_ID}:job-definition/job-1:2"


@pytest.mark.parametrize(
    ("candidate", "is_valid"),
    [
        (_FALLBACK_ARN_1, True),
        (
            "arn:custom:batch:service-region:service-account:job-definition/job-1:1",
            True,
        ),
        ("", False),
        (7, False),
        ("job-1:1", False),
        (
            "arn:aws:ecs:us-east-1:123456789012:job-definition/job-1:1",
            False,
        ),
        (
            "arn:aws:batch:us-east-1:123456789012:job-queue/job-1:1",
            False,
        ),
        (
            "arn:aws:batch:us-east-1:123456789012:job-definition/different-job:1",
            False,
        ),
        (
            "arn:aws:batch:us-east-1:123456789012:job-definition/job-1:latest",
            False,
        ),
        (
            "arn:aws:batch:us-east-1:123456789012:job-definition/job-1:１",
            False,
        ),
    ],
)
def test_exact_job_definition_arn_validates_structure_name_and_numeric_revision(
    candidate: object,
    is_valid: bool,
) -> None:
    assert _is_exact_job_definition_arn(candidate, "job-1") is is_valid


def test_cleanup_deregisters_exact_persisted_arn_before_clearing() -> None:
    events: list[str] = []
    batch_client = MagicMock()
    batch_client.describe_job_definitions.side_effect = lambda **_kwargs: (
        events.append("describe") or _ACTIVE_DEFINITION
    )
    batch_client.deregister_job_definition.side_effect = lambda **_kwargs: events.append("deregister")
    jobs_dao = MagicMock()
    jobs_dao.get_job.return_value = SimpleNamespace(batch_job_definition_arn="definition-arn")
    jobs_dao.clear_batch_job_definition.side_effect = lambda _request: events.append("clear")

    BatchJobDefinitionCleanup(batch_client, jobs_dao).cleanup(_REQUEST)

    assert events == ["describe", "deregister", "clear"]
    batch_client.describe_job_definitions.assert_called_once_with(jobDefinitions=["definition-arn"])
    batch_client.deregister_job_definition.assert_called_once_with(jobDefinition="definition-arn")
    clear_request = jobs_dao.clear_batch_job_definition.call_args.args[0]
    assert clear_request.job_definition_arn == "definition-arn"


def test_cleanup_ignores_unpersisted_request_arn_for_authority() -> None:
    batch_client = MagicMock()
    batch_client.describe_job_definitions.return_value = {"jobDefinitions": []}
    jobs_dao = MagicMock()
    jobs_dao.get_job.return_value = SimpleNamespace(batch_job_definition_arn=None)

    BatchJobDefinitionCleanup(batch_client, jobs_dao).cleanup(
        CleanupBatchJobDefinitionRequest(
            _TENANT,
            "connector-1",
            "job-1",
            "caller-controlled-arn",
        )
    )

    jobs_dao.get_job.assert_called_once()
    batch_client.describe_job_definitions.assert_called_once_with(
        jobDefinitionName="job-1",
        status="ACTIVE",
    )
    batch_client.deregister_job_definition.assert_not_called()
    jobs_dao.clear_batch_job_definition.assert_not_called()


def test_cleanup_without_persisted_arn_deregisters_cross_account_exact_name_arns() -> None:
    batch_client = MagicMock()
    batch_client.describe_job_definitions.side_effect = [
        {
            "jobDefinitions": [
                {
                    "jobDefinitionName": "job-1",
                    "jobDefinitionArn": _FALLBACK_ARN_1,
                },
                {
                    "jobDefinitionName": "different-job",
                    "jobDefinitionArn": ("arn:aws:batch:us-east-1:123456789012:job-definition/different-job:1"),
                },
                {
                    "jobDefinitionName": "job-1",
                    "jobDefinitionArn": "job-1:unqualified",
                },
            ],
            "nextToken": "opaque-token",
        },
        {
            "jobDefinitions": [
                {
                    "jobDefinitionName": "job-1",
                    "jobDefinitionArn": _FALLBACK_ARN_2,
                }
            ]
        },
    ]
    jobs_dao = MagicMock()
    jobs_dao.get_job.return_value = SimpleNamespace(batch_job_definition_arn=None)

    BatchJobDefinitionCleanup(batch_client, jobs_dao).cleanup(_REQUEST)

    assert batch_client.describe_job_definitions.call_args_list == [
        call(jobDefinitionName="job-1", status="ACTIVE"),
        call(
            jobDefinitionName="job-1",
            status="ACTIVE",
            nextToken="opaque-token",
        ),
    ]
    assert batch_client.deregister_job_definition.call_args_list == [
        call(jobDefinition=_FALLBACK_ARN_1),
        call(jobDefinition=_FALLBACK_ARN_2),
    ]
    jobs_dao.clear_batch_job_definition.assert_not_called()


def test_deregistration_failure_propagates_without_clearing_persisted_arn() -> None:
    batch_client = MagicMock()
    batch_client.describe_job_definitions.return_value = _ACTIVE_DEFINITION
    batch_client.deregister_job_definition.side_effect = RuntimeError("deregistration failed")
    jobs_dao = MagicMock()
    jobs_dao.get_job.return_value = SimpleNamespace(batch_job_definition_arn="definition-arn")

    with pytest.raises(RuntimeError, match="deregistration failed"):
        BatchJobDefinitionCleanup(batch_client, jobs_dao).cleanup(_REQUEST)

    jobs_dao.clear_batch_job_definition.assert_not_called()


def test_fallback_empty_results_are_idempotent_without_dao_clear() -> None:
    batch_client = MagicMock()
    batch_client.describe_job_definitions.return_value = {"jobDefinitions": []}
    jobs_dao = MagicMock()
    jobs_dao.get_job.return_value = SimpleNamespace(batch_job_definition_arn=None)
    cleanup = BatchJobDefinitionCleanup(batch_client, jobs_dao)

    cleanup.cleanup(_REQUEST)
    cleanup.cleanup(_REQUEST)

    assert batch_client.describe_job_definitions.call_args_list == [
        call(jobDefinitionName="job-1", status="ACTIVE"),
        call(jobDefinitionName="job-1", status="ACTIVE"),
    ]
    batch_client.deregister_job_definition.assert_not_called()
    jobs_dao.clear_batch_job_definition.assert_not_called()


def test_deregistration_retry_uses_same_persisted_arn_then_clears_once() -> None:
    batch_client = MagicMock()
    batch_client.describe_job_definitions.return_value = _ACTIVE_DEFINITION
    batch_client.deregister_job_definition.side_effect = [
        RuntimeError("deregistration failed"),
        {},
    ]
    jobs_dao = MagicMock()
    jobs_dao.get_job.return_value = SimpleNamespace(batch_job_definition_arn="definition-arn")
    cleanup = BatchJobDefinitionCleanup(batch_client, jobs_dao)

    with pytest.raises(RuntimeError, match="deregistration failed"):
        cleanup.cleanup(_REQUEST)

    jobs_dao.clear_batch_job_definition.assert_not_called()

    cleanup.cleanup(_REQUEST)

    assert batch_client.describe_job_definitions.call_args_list == [
        call(jobDefinitions=["definition-arn"]),
        call(jobDefinitions=["definition-arn"]),
    ]
    assert batch_client.deregister_job_definition.call_args_list == [
        call(jobDefinition="definition-arn"),
        call(jobDefinition="definition-arn"),
    ]
    jobs_dao.clear_batch_job_definition.assert_called_once()
    clear_request = jobs_dao.clear_batch_job_definition.call_args.args[0]
    assert clear_request.job_definition_arn == "definition-arn"


def test_clear_failure_retry_skips_second_deregistration_when_inactive() -> None:
    batch_client = MagicMock()
    batch_client.describe_job_definitions.side_effect = [
        _ACTIVE_DEFINITION,
        _INACTIVE_DEFINITION,
    ]
    jobs_dao = MagicMock()
    jobs_dao.get_job.return_value = SimpleNamespace(batch_job_definition_arn="definition-arn")
    jobs_dao.clear_batch_job_definition.side_effect = [
        RuntimeError("clear failed"),
        None,
    ]
    cleanup = BatchJobDefinitionCleanup(batch_client, jobs_dao)

    with pytest.raises(RuntimeError, match="clear failed"):
        cleanup.cleanup(_REQUEST)

    cleanup.cleanup(_REQUEST)

    batch_client.deregister_job_definition.assert_called_once_with(jobDefinition="definition-arn")
    clear_requests = [clear_call.args[0] for clear_call in jobs_dao.clear_batch_job_definition.call_args_list]
    assert [request.job_definition_arn for request in clear_requests] == [
        "definition-arn",
        "definition-arn",
    ]


def test_describe_failure_propagates_without_deregistering_or_clearing() -> None:
    batch_client = MagicMock()
    batch_client.describe_job_definitions.side_effect = RuntimeError("describe failed")
    jobs_dao = MagicMock()
    jobs_dao.get_job.return_value = SimpleNamespace(batch_job_definition_arn="definition-arn")

    with pytest.raises(RuntimeError, match="describe failed"):
        BatchJobDefinitionCleanup(batch_client, jobs_dao).cleanup(_REQUEST)

    batch_client.deregister_job_definition.assert_not_called()
    jobs_dao.clear_batch_job_definition.assert_not_called()


def test_fallback_describe_failure_propagates_without_dao_clear() -> None:
    batch_client = MagicMock()
    batch_client.describe_job_definitions.side_effect = RuntimeError("describe failed")
    jobs_dao = MagicMock()
    jobs_dao.get_job.return_value = SimpleNamespace(batch_job_definition_arn=None)

    with pytest.raises(RuntimeError, match="describe failed"):
        BatchJobDefinitionCleanup(batch_client, jobs_dao).cleanup(_REQUEST)

    batch_client.deregister_job_definition.assert_not_called()
    jobs_dao.clear_batch_job_definition.assert_not_called()


def test_fallback_deregistration_failure_propagates_without_dao_clear() -> None:
    batch_client = MagicMock()
    batch_client.describe_job_definitions.return_value = {
        "jobDefinitions": [
            {
                "jobDefinitionName": "job-1",
                "jobDefinitionArn": _FALLBACK_ARN_1,
            }
        ]
    }
    batch_client.deregister_job_definition.side_effect = RuntimeError("deregistration failed")
    jobs_dao = MagicMock()
    jobs_dao.get_job.return_value = SimpleNamespace(batch_job_definition_arn=None)

    with pytest.raises(RuntimeError, match="deregistration failed"):
        BatchJobDefinitionCleanup(batch_client, jobs_dao).cleanup(_REQUEST)

    jobs_dao.clear_batch_job_definition.assert_not_called()


@pytest.mark.parametrize(
    "described_definition",
    [
        {"jobDefinitionArn": "definition-arn"},
        {"jobDefinitionArn": "definition-arn", "status": "UNKNOWN"},
    ],
    ids=["missing", "unknown"],
)
def test_missing_or_unknown_status_fails_before_deregistering_or_clearing(
    described_definition: dict[str, str],
) -> None:
    batch_client = MagicMock()
    batch_client.describe_job_definitions.return_value = {"jobDefinitions": [described_definition]}
    jobs_dao = MagicMock()
    jobs_dao.get_job.return_value = SimpleNamespace(batch_job_definition_arn="definition-arn")

    with pytest.raises(ValueError):
        BatchJobDefinitionCleanup(batch_client, jobs_dao).cleanup(_REQUEST)

    batch_client.deregister_job_definition.assert_not_called()
    jobs_dao.clear_batch_job_definition.assert_not_called()


@pytest.mark.parametrize(
    "job_definitions",
    [
        [],
        [{"jobDefinitionArn": "different-arn", "status": "ACTIVE"}],
    ],
    ids=["retention-deleted", "different-definition"],
)
def test_definition_no_longer_returned_proceeds_directly_to_exact_clear(
    job_definitions: list[dict[str, str]],
) -> None:
    batch_client = MagicMock()
    batch_client.describe_job_definitions.return_value = {"jobDefinitions": job_definitions}
    jobs_dao = MagicMock()
    jobs_dao.get_job.return_value = SimpleNamespace(batch_job_definition_arn="definition-arn")

    BatchJobDefinitionCleanup(batch_client, jobs_dao).cleanup(_REQUEST)

    batch_client.deregister_job_definition.assert_not_called()
    jobs_dao.clear_batch_job_definition.assert_called_once()
    clear_request = jobs_dao.clear_batch_job_definition.call_args.args[0]
    assert clear_request.job_definition_arn == "definition-arn"
