"""Service-owned Batch orchestration tests."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from aws_lambda_powertools.utilities.data_classes.dynamo_db_stream_event import DynamoDBRecord
from botocore.exceptions import ClientError
from ccf_api_models.models import (
    ConnectorStatus,
    ContainerProperties,
    JobStatus,
    ResourceRequirements,
    SourceAccessConfiguration,
)

import job_orchestrator_handler
from common.batch_submission import BatchSubmissionRecoveryError, SubmittedExecutionLocator
from common.storage.ddb.custom_connector_jobs_dao import (
    BatchJobDefinitionRequest,
    BatchSubmissionIdentityRequest,
    StartFailureCompensationRequest,
    UpdateJobStatusRequest,
)
from common.storage.ddb.custom_connectors_dao import StoredConnector
from common.tenant import TenantContext

_TENANT = TenantContext("123456789012", "us-west-2")
_CONTEXT = job_orchestrator_handler.JobStartContext(
    job_id="job-1",
    connector_id="connector-1",
    custom_connector_arn_prefix=_TENANT.get_arn_prefix(),
    tenant_context=_TENANT,
)
_EXECUTION_ROLE_ARN = "arn:aws:iam::123456789012:role/CcfFrameworkExecutionRole"
_RUNTIME_ROLE_ARN = "arn:aws:iam::123456789012:role/ccf-runtime/CcfConnectorRuntime-0123456789abcdef0123456789abcdef"
_JOB_QUEUE = "CcfBatchJobQueue"
_BATCH_ACCOUNT_ID = "210987654321"
_JOB_DEFINITION_ARN = f"arn:aws:batch:us-west-2:{_BATCH_ACCOUNT_ID}:job-definition/job-1:7"


def stored_connector() -> StoredConnector:
    return StoredConnector(
        connector_id="connector-1",
        arn=_TENANT.get_connector_arn("connector-1"),
        name="private-connector-name",
        created_at=1.0,
        updated_at=1.0,
        status=ConnectorStatus.IN_USE,
        container_properties=ContainerProperties(
            image_uri="123456789012.dkr.ecr.us-west-2.amazonaws.com/connector:latest",
            command=["ccf-connector", "run", "connector:definition"],
            resource_requirements=ResourceRequirements(cpu=1.0, memory=2048),
            timeout=7200,
        ),
        encrypted_configuration=b"ciphertext",
        source_access=SourceAccessConfiguration(
            role_arn="arn:aws:iam::210987654321:role/exact-source",
            external_id="connector-external-id",
        ),
        runtime_role_arn=_RUNTIME_ROLE_ARN,
    )


def test_record_handler_started_status_uses_service_identity_only() -> None:
    with patch("job_orchestrator_handler.handle_job_start") as handle_job_start:
        record = MagicMock(spec=DynamoDBRecord)
        record.dynamodb.new_image = {
            "job_id": "job-1",
            "connector_id": "connector-1",
            "custom_connector_arn_prefix": _TENANT.get_arn_prefix(),
            "status": JobStatus.STARTED.value,
            "environment": [{"name": "UNTRUSTED", "value": "ignored"}],
        }

        job_orchestrator_handler.record_handler(record)

    context = handle_job_start.call_args.args[0]
    assert context == _CONTEXT
    assert not hasattr(context, "environment")


def test_handle_job_start_accepts_cross_account_definition_and_service_values() -> None:
    assert _BATCH_ACCOUNT_ID != _TENANT.account_id
    operation_order: list[str] = []
    batch_client = MagicMock()

    def register_job_definition(**_kwargs: object) -> dict[str, str]:
        operation_order.append("register")
        return {"jobDefinitionArn": _JOB_DEFINITION_ARN}

    def submit_job(**kwargs: object) -> dict[str, str]:
        assert kwargs["jobDefinition"] == _JOB_DEFINITION_ARN
        operation_order.append("submit")
        return {"jobId": "batch-1"}

    batch_client.register_job_definition.side_effect = register_job_definition
    batch_client.submit_job.side_effect = submit_job
    jobs_dao = MagicMock()
    jobs_dao.claim_start_orchestration.return_value = True

    def store_batch_submission_identity(request: BatchSubmissionIdentityRequest) -> None:
        assert request.job_queue == _JOB_QUEUE
        assert request.job_name == "job-1"
        operation_order.append("persist-submission")

    jobs_dao.store_batch_submission_identity.side_effect = store_batch_submission_identity

    def store_batch_job_definition(request: BatchJobDefinitionRequest) -> None:
        assert request.job_definition_arn == _JOB_DEFINITION_ARN
        operation_order.append("persist")

    def update_job_status(request: UpdateJobStatusRequest) -> None:
        assert request.status is JobStatus.RUNNING
        assert request.batch_job_id == "batch-1"
        assert request.service_execution is not None
        assert request.service_execution.execution_id == "batch-1"
        assert request.service_execution.execution_role_arn == _EXECUTION_ROLE_ARN
        assert request.service_execution.job_role_arn == _RUNTIME_ROLE_ARN
        assert request.service_execution.execution_role_arn != request.service_execution.job_role_arn
        operation_order.append("status:RUNNING")

    jobs_dao.store_batch_job_definition.side_effect = store_batch_job_definition
    jobs_dao.update_job_status.side_effect = update_job_status
    connectors_dao = MagicMock()
    connectors_dao.get_connector.return_value = stored_connector()

    with (
        patch.object(job_orchestrator_handler, "batch_client", batch_client),
        patch.object(job_orchestrator_handler, "jobs_dao", jobs_dao),
        patch.object(job_orchestrator_handler, "connectors_dao", connectors_dao),
        patch.object(job_orchestrator_handler, "FRAMEWORK_EXECUTION_ROLE_ARN", _EXECUTION_ROLE_ARN),
        patch.object(job_orchestrator_handler, "CUSTOM_CONNECTOR_API_ENDPOINT", "https://ccf.example.com"),
        patch.object(job_orchestrator_handler, "AWS_BATCH_JOB_QUEUE", _JOB_QUEUE),
    ):
        job_orchestrator_handler.handle_job_start(_CONTEXT)

    properties = batch_client.register_job_definition.call_args.kwargs["containerProperties"]
    assert properties["command"] == ["ccf-connector", "run", "connector:definition"]
    assert properties["executionRoleArn"] == _EXECUTION_ROLE_ARN
    assert properties["jobRoleArn"] == _RUNTIME_ROLE_ARN
    environment = {entry["name"]: entry["value"] for entry in properties["environment"]}
    assert environment == {
        "CUSTOM_CONNECTOR_JOB_ID": "job-1",
        "CUSTOM_CONNECTOR_FRAMEWORK_CUSTOM_CONNECTOR_ID": "connector-1",
        "CUSTOM_CONNECTOR_FRAMEWORK_CUSTOM_CONNECTOR_ARN_PREFIX": _TENANT.get_arn_prefix(),
        "CUSTOM_CONNECTOR_FRAMEWORK_API_ENDPOINT": "https://ccf.example.com",
        "AWS_REGION": "us-west-2",
    }
    assert "private-connector-name" not in repr(batch_client.mock_calls)
    submit = batch_client.submit_job.call_args.kwargs
    assert submit["jobDefinition"] == _JOB_DEFINITION_ARN
    assert submit["jobQueue"] == _JOB_QUEUE
    assert submit["jobName"] == "job-1"
    assert submit["timeout"] == {"attemptDurationSeconds": 7200}
    assert submit["tags"] == {"ccf:owned-by": "custom-connector-framework"}
    assert operation_order == [
        "register",
        "persist",
        "persist-submission",
        "submit",
        "status:RUNNING",
    ]


@pytest.mark.parametrize(
    "job_definition_arn",
    [_JOB_DEFINITION_ARN, None],
    ids=["persisted-arn", "no-persisted-arn"],
)
def test_interrupted_claimed_start_enters_compensation_before_terminal_cleanup(
    job_definition_arn: str | None,
) -> None:
    events: list[str] = []
    batch_client = MagicMock()
    jobs_dao = MagicMock()
    jobs_dao.claim_start_orchestration.return_value = False
    jobs_dao.get_job.return_value = SimpleNamespace(
        status=JobStatus.STARTED,
        batch_job_id=None,
        batch_job_definition_arn=job_definition_arn,
        batch_job_queue=None,
        batch_job_name=None,
        start_orchestration_claimed_at=1.0,
        start_failure_compensation_started_at=None,
    )
    jobs_dao.mark_start_failure_compensation.side_effect = lambda _request: events.append("compensation")
    jobs_dao.update_job_status.side_effect = lambda _request: events.append("status:FAILED")
    connectors_dao = MagicMock()
    cleanup = MagicMock()
    cleanup.cleanup.side_effect = lambda _request: events.append("cleanup")

    with (
        patch.object(job_orchestrator_handler, "batch_client", batch_client),
        patch.object(job_orchestrator_handler, "jobs_dao", jobs_dao),
        patch.object(job_orchestrator_handler, "connectors_dao", connectors_dao),
        patch.object(job_orchestrator_handler, "batch_job_definition_cleanup", cleanup),
    ):
        job_orchestrator_handler.handle_job_start(_CONTEXT)

    assert events == ["compensation", "status:FAILED", "cleanup"]
    compensation_request = jobs_dao.mark_start_failure_compensation.call_args.args[0]
    assert compensation_request.batch_job_id is None
    assert compensation_request.job_definition_arn == job_definition_arn
    status_request = jobs_dao.update_job_status.call_args.args[0]
    assert status_request.status is JobStatus.FAILED
    cleanup_request = cleanup.cleanup.call_args.args[0]
    assert cleanup_request.job_definition_arn == job_definition_arn
    connectors_dao.get_connector.assert_not_called()
    batch_client.register_job_definition.assert_not_called()
    batch_client.submit_job.assert_not_called()
    batch_client.terminate_job.assert_not_called()


def test_stale_started_delivery_after_running_status_skips_batch_side_effects() -> None:
    batch_client = MagicMock()
    jobs_dao = MagicMock()
    jobs_dao.claim_start_orchestration.return_value = False
    jobs_dao.get_job.return_value = SimpleNamespace(
        status=JobStatus.RUNNING,
        batch_job_id="batch-1",
        batch_job_definition_arn=_JOB_DEFINITION_ARN,
        batch_job_queue=_JOB_QUEUE,
        batch_job_name="job-1",
        start_orchestration_claimed_at=1.0,
        start_failure_compensation_started_at=None,
    )
    connectors_dao = MagicMock()
    cleanup = MagicMock()

    with (
        patch.object(job_orchestrator_handler, "batch_client", batch_client),
        patch.object(job_orchestrator_handler, "jobs_dao", jobs_dao),
        patch.object(job_orchestrator_handler, "connectors_dao", connectors_dao),
        patch.object(job_orchestrator_handler, "batch_job_definition_cleanup", cleanup),
    ):
        job_orchestrator_handler.handle_job_start(_CONTEXT)

    connectors_dao.get_connector.assert_not_called()
    batch_client.register_job_definition.assert_not_called()
    batch_client.submit_job.assert_not_called()
    jobs_dao.update_job_status.assert_not_called()
    cleanup.cleanup.assert_not_called()


def test_claim_failure_propagates_without_failure_marking_or_batch_side_effects() -> None:
    claim_error = RuntimeError("claim failed")
    batch_client = MagicMock()
    jobs_dao = MagicMock()
    jobs_dao.claim_start_orchestration.side_effect = claim_error
    connectors_dao = MagicMock()

    with (
        patch.object(job_orchestrator_handler, "batch_client", batch_client),
        patch.object(job_orchestrator_handler, "jobs_dao", jobs_dao),
        patch.object(job_orchestrator_handler, "connectors_dao", connectors_dao),
        pytest.raises(RuntimeError) as raised_error,
    ):
        job_orchestrator_handler.handle_job_start(_CONTEXT)

    assert raised_error.value is claim_error
    jobs_dao.get_job.assert_not_called()
    jobs_dao.update_job_status.assert_not_called()
    connectors_dao.get_connector.assert_not_called()
    batch_client.register_job_definition.assert_not_called()
    batch_client.submit_job.assert_not_called()
    batch_client.terminate_job.assert_not_called()


def test_running_persistence_failure_terminates_before_failed_transition() -> None:
    batch_client = MagicMock()
    batch_client.register_job_definition.return_value = {"jobDefinitionArn": _JOB_DEFINITION_ARN}
    batch_client.submit_job.return_value = {"jobId": "batch-1"}
    jobs_dao = MagicMock()
    jobs_dao.claim_start_orchestration.return_value = True
    operation_order = []

    def update_job_status(request: UpdateJobStatusRequest) -> None:
        operation_order.append(f"status:{request.status.value}")
        if request.status is JobStatus.RUNNING:
            raise RuntimeError("status write failed")

    jobs_dao.update_job_status.side_effect = update_job_status
    jobs_dao.mark_start_failure_compensation.side_effect = lambda _request: operation_order.append("compensation")
    batch_client.terminate_job.side_effect = lambda **_kwargs: operation_order.append("terminate")
    job_definition_cleanup = MagicMock()
    job_definition_cleanup.cleanup.side_effect = lambda _request: operation_order.append("cleanup")
    connectors_dao = MagicMock()
    connectors_dao.get_connector.return_value = stored_connector()

    with (
        patch.object(job_orchestrator_handler, "batch_client", batch_client),
        patch.object(job_orchestrator_handler, "jobs_dao", jobs_dao),
        patch.object(job_orchestrator_handler, "connectors_dao", connectors_dao),
        patch.object(
            job_orchestrator_handler,
            "batch_job_definition_cleanup",
            job_definition_cleanup,
        ),
        patch.object(job_orchestrator_handler, "FRAMEWORK_EXECUTION_ROLE_ARN", _EXECUTION_ROLE_ARN),
    ):
        job_orchestrator_handler.handle_job_start(_CONTEXT)

    assert operation_order == [
        "status:RUNNING",
        "compensation",
        "terminate",
        "status:FAILED",
        "cleanup",
    ]
    batch_client.terminate_job.assert_called_once_with(
        jobId="batch-1",
        reason="CCF could not persist the submitted Batch execution",
    )
    status_requests = [mock_call.args[0] for mock_call in jobs_dao.update_job_status.call_args_list]
    assert status_requests[0].batch_job_id == "batch-1"
    assert status_requests[1].batch_job_id == "batch-1"


def _lost_submit_response_start(
    list_jobs_pages: list[dict[str, object]] | Exception,
) -> tuple[MagicMock, MagicMock, MagicMock]:
    """Run one start whose SubmitJob response was lost, with a Batch listing double."""
    batch_client = MagicMock()
    batch_client.register_job_definition.return_value = {"jobDefinitionArn": _JOB_DEFINITION_ARN}
    batch_client.submit_job.side_effect = RuntimeError("submit response lost")
    if isinstance(list_jobs_pages, Exception):
        batch_client.list_jobs.side_effect = list_jobs_pages
    else:
        batch_client.list_jobs.side_effect = list_jobs_pages
    jobs_dao = MagicMock()
    jobs_dao.claim_start_orchestration.return_value = True
    job_definition_cleanup = MagicMock()
    connectors_dao = MagicMock()
    connectors_dao.get_connector.return_value = stored_connector()

    with (
        patch.object(job_orchestrator_handler, "batch_client", batch_client),
        patch.object(job_orchestrator_handler, "jobs_dao", jobs_dao),
        patch.object(job_orchestrator_handler, "connectors_dao", connectors_dao),
        patch.object(
            job_orchestrator_handler,
            "batch_job_definition_cleanup",
            job_definition_cleanup,
        ),
        patch.object(job_orchestrator_handler, "AWS_BATCH_JOB_QUEUE", _JOB_QUEUE),
        patch.object(
            job_orchestrator_handler,
            "submitted_execution_locator",
            SubmittedExecutionLocator(batch_client),
        ),
    ):
        job_orchestrator_handler.handle_job_start(_CONTEXT)
    return batch_client, jobs_dao, job_definition_cleanup


def test_lost_submit_response_terminates_the_one_proven_owned_execution() -> None:
    batch_client, jobs_dao, job_definition_cleanup = _lost_submit_response_start(
        [
            {
                "jobSummaryList": [
                    {
                        "jobId": "batch-other-name",
                        "jobName": "job-10",
                        "jobDefinition": _JOB_DEFINITION_ARN,
                    },
                    {
                        "jobId": "batch-other-definition",
                        "jobName": "job-1",
                        "jobDefinition": _JOB_DEFINITION_ARN.replace(":7", ":6"),
                    },
                ],
                "nextToken": "page-2",
            },
            {
                "jobSummaryList": [
                    {
                        "jobId": "batch-owned",
                        "jobName": "job-1",
                        "jobDefinition": _JOB_DEFINITION_ARN,
                    }
                ]
            },
        ]
    )

    assert batch_client.list_jobs.call_args_list[0].kwargs == {
        "jobQueue": _JOB_QUEUE,
        "filters": [{"name": "JOB_NAME", "values": ["job-1"]}],
    }
    assert batch_client.list_jobs.call_args_list[1].kwargs["nextToken"] == "page-2"
    compensation_request = jobs_dao.mark_start_failure_compensation.call_args.args[0]
    assert compensation_request.batch_job_id == "batch-owned"
    batch_client.terminate_job.assert_called_once_with(
        jobId="batch-owned",
        reason="CCF could not persist the submitted Batch execution",
    )
    status_request = jobs_dao.update_job_status.call_args.args[0]
    assert status_request.status is JobStatus.FAILED
    assert status_request.batch_job_id == "batch-owned"
    assert job_definition_cleanup.cleanup.call_args.args[0].job_definition_arn == _JOB_DEFINITION_ARN


@pytest.mark.parametrize(
    "list_jobs_pages",
    [
        [{"jobSummaryList": []}],
        [
            {
                "jobSummaryList": [
                    {"jobId": "batch-a", "jobName": "job-1", "jobDefinition": _JOB_DEFINITION_ARN},
                    {"jobId": "batch-b", "jobName": "job-1", "jobDefinition": _JOB_DEFINITION_ARN},
                ]
            }
        ],
        RuntimeError("list jobs unavailable"),
    ],
    ids=["zero-owned", "multiple-owned", "unverifiable"],
)
def test_unprovable_lost_submit_response_fails_closed_without_releasing_the_job(
    list_jobs_pages: list[dict[str, object]] | Exception,
) -> None:
    with pytest.raises(BatchSubmissionRecoveryError):
        _lost_submit_response_start(list_jobs_pages)


def test_rejected_submission_does_not_search_for_an_execution() -> None:
    batch_client = MagicMock()
    batch_client.register_job_definition.return_value = {"jobDefinitionArn": _JOB_DEFINITION_ARN}
    batch_client.submit_job.side_effect = ClientError(
        {
            "Error": {"Code": "ClientException", "Message": "invalid parameter"},
            "ResponseMetadata": {"HTTPStatusCode": 400},
        },
        "SubmitJob",
    )
    jobs_dao = MagicMock()
    jobs_dao.claim_start_orchestration.return_value = True
    job_definition_cleanup = MagicMock()
    connectors_dao = MagicMock()
    connectors_dao.get_connector.return_value = stored_connector()

    with (
        patch.object(job_orchestrator_handler, "batch_client", batch_client),
        patch.object(job_orchestrator_handler, "jobs_dao", jobs_dao),
        patch.object(job_orchestrator_handler, "connectors_dao", connectors_dao),
        patch.object(
            job_orchestrator_handler,
            "batch_job_definition_cleanup",
            job_definition_cleanup,
        ),
        patch.object(job_orchestrator_handler, "AWS_BATCH_JOB_QUEUE", _JOB_QUEUE),
    ):
        job_orchestrator_handler.handle_job_start(_CONTEXT)

    batch_client.list_jobs.assert_not_called()
    batch_client.terminate_job.assert_not_called()
    status_request = jobs_dao.update_job_status.call_args.args[0]
    assert status_request.status is JobStatus.FAILED
    assert status_request.batch_job_id is None


def test_replay_recovers_the_persisted_submission_identity() -> None:
    batch_client = MagicMock()
    batch_client.list_jobs.return_value = {
        "jobSummaryList": [{"jobId": "batch-owned", "jobName": "job-1", "jobDefinition": _JOB_DEFINITION_ARN}]
    }
    jobs_dao = MagicMock()
    jobs_dao.claim_start_orchestration.return_value = False
    jobs_dao.get_job.return_value = SimpleNamespace(
        status=JobStatus.STARTED,
        batch_job_id=None,
        batch_job_definition_arn=_JOB_DEFINITION_ARN,
        batch_job_queue=_JOB_QUEUE,
        batch_job_name="job-1",
        start_orchestration_claimed_at=1.0,
        start_failure_compensation_started_at=2.0,
    )
    cleanup = MagicMock()

    with (
        patch.object(job_orchestrator_handler, "batch_client", batch_client),
        patch.object(job_orchestrator_handler, "jobs_dao", jobs_dao),
        patch.object(job_orchestrator_handler, "batch_job_definition_cleanup", cleanup),
        patch.object(
            job_orchestrator_handler,
            "submitted_execution_locator",
            SubmittedExecutionLocator(batch_client),
        ),
    ):
        job_orchestrator_handler.handle_job_start(_CONTEXT)

    assert jobs_dao.mark_start_failure_compensation.call_args.args[0].batch_job_id == "batch-owned"
    batch_client.terminate_job.assert_called_once_with(
        jobId="batch-owned",
        reason="CCF could not persist the submitted Batch execution",
    )
    assert jobs_dao.update_job_status.call_args.args[0].batch_job_id == "batch-owned"


@pytest.mark.parametrize(
    ("register_response", "register_error"),
    [
        (None, RuntimeError("registration failed")),
        ({}, None),
        ({"jobDefinitionArn": ""}, None),
        ({"jobDefinitionArn": 7}, None),
        ({"jobDefinitionArn": "job-1:7"}, None),
        (
            {"jobDefinitionArn": ("arn:aws:batch:us-west-2:123456789012:job-definition/different-job:7")},
            None,
        ),
    ],
    ids=[
        "registration-failure",
        "missing-arn",
        "blank-arn",
        "non-string-arn",
        "unqualified-arn",
        "wrong-name-arn",
    ],
)
def test_registration_failure_persists_failed_before_fallback_cleanup(
    register_response: dict[str, object] | None,
    register_error: RuntimeError | None,
) -> None:
    events: list[str] = []
    batch_client = MagicMock()
    if register_error is not None:
        batch_client.register_job_definition.side_effect = register_error
    else:
        batch_client.register_job_definition.return_value = register_response
    jobs_dao = MagicMock()
    jobs_dao.claim_start_orchestration.return_value = True
    jobs_dao.mark_start_failure_compensation.side_effect = lambda _request: events.append("compensation")
    jobs_dao.update_job_status.side_effect = lambda _request: events.append("status:FAILED")
    connectors_dao = MagicMock()
    connectors_dao.get_connector.return_value = stored_connector()
    cleanup = MagicMock()
    cleanup.cleanup.side_effect = lambda _request: events.append("cleanup")

    with (
        patch.object(job_orchestrator_handler, "batch_client", batch_client),
        patch.object(job_orchestrator_handler, "jobs_dao", jobs_dao),
        patch.object(job_orchestrator_handler, "connectors_dao", connectors_dao),
        patch.object(job_orchestrator_handler, "batch_job_definition_cleanup", cleanup),
    ):
        job_orchestrator_handler.handle_job_start(_CONTEXT)

    assert events == ["compensation", "status:FAILED", "cleanup"]
    compensation_request = jobs_dao.mark_start_failure_compensation.call_args.args[0]
    assert compensation_request.job_definition_arn is None
    status_request = jobs_dao.update_job_status.call_args.args[0]
    assert status_request.status is JobStatus.FAILED
    assert status_request.batch_job_id is None
    jobs_dao.store_batch_job_definition.assert_not_called()
    batch_client.submit_job.assert_not_called()
    batch_client.terminate_job.assert_not_called()
    cleanup_request = cleanup.cleanup.call_args.args[0]
    assert cleanup_request.job_definition_arn is None


@pytest.mark.parametrize(
    "job_definition_arn",
    [_JOB_DEFINITION_ARN, None],
    ids=["persisted-arn", "no-persisted-arn"],
)
def test_terminal_start_replay_retries_cleanup_without_failure_marking(
    job_definition_arn: str | None,
) -> None:
    batch_client = MagicMock()
    jobs_dao = MagicMock()
    jobs_dao.claim_start_orchestration.return_value = False
    jobs_dao.get_job.return_value = SimpleNamespace(
        status=JobStatus.FAILED,
        batch_job_definition_arn=job_definition_arn,
        batch_job_queue=None,
        batch_job_name=None,
        start_failure_compensation_started_at=None,
    )
    connectors_dao = MagicMock()
    cleanup = MagicMock()

    with (
        patch.object(job_orchestrator_handler, "batch_client", batch_client),
        patch.object(job_orchestrator_handler, "jobs_dao", jobs_dao),
        patch.object(job_orchestrator_handler, "connectors_dao", connectors_dao),
        patch.object(job_orchestrator_handler, "batch_job_definition_cleanup", cleanup),
    ):
        job_orchestrator_handler.handle_job_start(_CONTEXT)
        job_orchestrator_handler.handle_job_start(_CONTEXT)

    assert [cleanup_call.args[0].job_definition_arn for cleanup_call in cleanup.cleanup.call_args_list] == [
        job_definition_arn,
        job_definition_arn,
    ]
    jobs_dao.update_job_status.assert_not_called()
    connectors_dao.get_connector.assert_not_called()
    batch_client.register_job_definition.assert_not_called()
    batch_client.submit_job.assert_not_called()
    batch_client.terminate_job.assert_not_called()


def test_start_compensation_termination_failure_stays_started_and_propagates() -> None:
    termination_error = RuntimeError("termination failed")
    batch_client = MagicMock()
    batch_client.register_job_definition.return_value = {"jobDefinitionArn": _JOB_DEFINITION_ARN}
    batch_client.submit_job.return_value = {"jobId": "batch-1"}
    batch_client.terminate_job.side_effect = termination_error
    jobs_dao = MagicMock()
    jobs_dao.claim_start_orchestration.return_value = True

    def update_job_status(request: UpdateJobStatusRequest) -> None:
        if request.status is JobStatus.RUNNING:
            raise RuntimeError("status write failed")

    jobs_dao.update_job_status.side_effect = update_job_status
    job_definition_cleanup = MagicMock()
    connectors_dao = MagicMock()
    connectors_dao.get_connector.return_value = stored_connector()

    with (
        patch.object(job_orchestrator_handler, "batch_client", batch_client),
        patch.object(job_orchestrator_handler, "jobs_dao", jobs_dao),
        patch.object(job_orchestrator_handler, "connectors_dao", connectors_dao),
        patch.object(
            job_orchestrator_handler,
            "batch_job_definition_cleanup",
            job_definition_cleanup,
        ),
        patch.object(job_orchestrator_handler, "FRAMEWORK_EXECUTION_ROLE_ARN", _EXECUTION_ROLE_ARN),
        pytest.raises(RuntimeError) as raised_error,
    ):
        job_orchestrator_handler.handle_job_start(_CONTEXT)

    assert raised_error.value is termination_error
    jobs_dao.mark_start_failure_compensation.assert_called_once()
    status_requests = [mock_call.args[0] for mock_call in jobs_dao.update_job_status.call_args_list]
    assert [request.status for request in status_requests] == [JobStatus.RUNNING]
    job_definition_cleanup.cleanup.assert_not_called()


def test_handle_job_stop_requests_batch_termination() -> None:
    batch_client = MagicMock()
    jobs_dao = MagicMock()
    jobs_dao.get_job.return_value = SimpleNamespace(
        status=JobStatus.STOPPING,
        batch_job_definition_arn=_JOB_DEFINITION_ARN,
        start_failure_compensation_started_at=None,
    )

    with (
        patch.object(job_orchestrator_handler, "batch_client", batch_client),
        patch.object(job_orchestrator_handler, "jobs_dao", jobs_dao),
    ):
        job_orchestrator_handler.handle_job_stop(
            "job-1",
            "connector-1",
            "batch-1",
            _TENANT,
        )

    batch_client.terminate_job.assert_called_once_with(
        jobId="batch-1",
        reason="Job stopped by user via Custom Connector Framework API",
    )


def test_terminal_stop_replay_retries_fallback_cleanup_without_termination() -> None:
    batch_client = MagicMock()
    jobs_dao = MagicMock()
    jobs_dao.get_job.return_value = SimpleNamespace(
        status=JobStatus.STOPPED,
        batch_job_definition_arn=None,
    )
    cleanup = MagicMock()

    with (
        patch.object(job_orchestrator_handler, "batch_client", batch_client),
        patch.object(job_orchestrator_handler, "jobs_dao", jobs_dao),
        patch.object(job_orchestrator_handler, "batch_job_definition_cleanup", cleanup),
    ):
        job_orchestrator_handler.handle_job_stop(
            "job-1",
            "connector-1",
            None,
            _TENANT,
        )
        job_orchestrator_handler.handle_job_stop(
            "job-1",
            "connector-1",
            None,
            _TENANT,
        )

    assert cleanup.cleanup.call_count == 2
    assert all(cleanup_call.args[0].job_definition_arn is None for cleanup_call in cleanup.cleanup.call_args_list)
    batch_client.terminate_job.assert_not_called()
    jobs_dao.update_job_status.assert_not_called()


def test_handle_job_stop_without_batch_job_transitions_to_stopped() -> None:
    batch_client = MagicMock()
    jobs_dao = MagicMock()
    jobs_dao.get_job.return_value = SimpleNamespace(
        status=JobStatus.STOPPING,
        batch_job_definition_arn=_JOB_DEFINITION_ARN,
        start_failure_compensation_started_at=None,
    )
    job_definition_cleanup = MagicMock()

    with (
        patch.object(job_orchestrator_handler, "batch_client", batch_client),
        patch.object(job_orchestrator_handler, "jobs_dao", jobs_dao),
        patch.object(
            job_orchestrator_handler,
            "batch_job_definition_cleanup",
            job_definition_cleanup,
        ),
    ):
        job_orchestrator_handler.handle_job_stop(
            "job-1",
            "connector-1",
            None,
            _TENANT,
        )

    status_request = jobs_dao.update_job_status.call_args.args[0]
    assert status_request.status is JobStatus.STOPPED
    assert status_request.batch_job_id is None
    assert batch_client.mock_calls == []
    cleanup_request = job_definition_cleanup.cleanup.call_args.args[0]
    assert cleanup_request.job_definition_arn == _JOB_DEFINITION_ARN


def test_stop_termination_failure_stays_stopping_and_propagates_for_retry() -> None:
    termination_error = RuntimeError("termination failed")
    batch_client = MagicMock()
    batch_client.terminate_job.side_effect = termination_error
    stored_job = SimpleNamespace(
        status=JobStatus.STOPPING,
        batch_job_definition_arn=_JOB_DEFINITION_ARN,
    )
    jobs_dao = MagicMock()
    jobs_dao.get_job.return_value = stored_job
    job_definition_cleanup = MagicMock()

    with (
        patch.object(job_orchestrator_handler, "batch_client", batch_client),
        patch.object(job_orchestrator_handler, "jobs_dao", jobs_dao),
        patch.object(
            job_orchestrator_handler,
            "batch_job_definition_cleanup",
            job_definition_cleanup,
        ),
        pytest.raises(RuntimeError) as raised_error,
    ):
        job_orchestrator_handler.handle_job_stop(
            "job-1",
            "connector-1",
            "batch-1",
            _TENANT,
        )

    assert raised_error.value is termination_error
    assert stored_job.status is JobStatus.STOPPING
    assert stored_job.batch_job_definition_arn == _JOB_DEFINITION_ARN
    jobs_dao.update_job_status.assert_not_called()
    job_definition_cleanup.cleanup.assert_not_called()


def test_definition_persistence_failure_persists_failed_before_direct_cleanup() -> None:
    events: list[str] = []
    batch_client = MagicMock()
    batch_client.register_job_definition.return_value = {"jobDefinitionArn": _JOB_DEFINITION_ARN}
    jobs_dao = MagicMock()
    jobs_dao.claim_start_orchestration.return_value = True
    jobs_dao.store_batch_job_definition.side_effect = RuntimeError("persistence failed")
    jobs_dao.mark_start_failure_compensation.side_effect = lambda _request: events.append("compensation")
    jobs_dao.update_job_status.side_effect = lambda _request: events.append("status:FAILED")
    connectors_dao = MagicMock()
    connectors_dao.get_connector.return_value = stored_connector()
    cleanup = MagicMock()
    cleanup.cleanup.side_effect = lambda _request: events.append("cleanup")

    with (
        patch.object(job_orchestrator_handler, "batch_client", batch_client),
        patch.object(job_orchestrator_handler, "jobs_dao", jobs_dao),
        patch.object(job_orchestrator_handler, "connectors_dao", connectors_dao),
        patch.object(job_orchestrator_handler, "batch_job_definition_cleanup", cleanup),
    ):
        job_orchestrator_handler.handle_job_start(_CONTEXT)

    assert events == ["compensation", "status:FAILED", "cleanup"]
    batch_client.submit_job.assert_not_called()
    failed_request = jobs_dao.update_job_status.call_args.args[0]
    assert failed_request.status is JobStatus.FAILED
    cleanup_request = cleanup.cleanup.call_args.args[0]
    assert cleanup_request.job_definition_arn == _JOB_DEFINITION_ARN


def test_definition_persistence_failure_stops_when_compensation_persistence_fails() -> None:
    compensation_error = RuntimeError("compensation persistence failed")
    batch_client = MagicMock()
    batch_client.register_job_definition.return_value = {"jobDefinitionArn": _JOB_DEFINITION_ARN}
    jobs_dao = MagicMock()
    jobs_dao.claim_start_orchestration.return_value = True
    jobs_dao.store_batch_job_definition.side_effect = RuntimeError("persistence failed")
    jobs_dao.mark_start_failure_compensation.side_effect = compensation_error
    connectors_dao = MagicMock()
    connectors_dao.get_connector.return_value = stored_connector()
    cleanup = MagicMock()

    with (
        patch.object(job_orchestrator_handler, "batch_client", batch_client),
        patch.object(job_orchestrator_handler, "jobs_dao", jobs_dao),
        patch.object(job_orchestrator_handler, "connectors_dao", connectors_dao),
        patch.object(job_orchestrator_handler, "batch_job_definition_cleanup", cleanup),
        pytest.raises(RuntimeError) as raised_error,
    ):
        job_orchestrator_handler.handle_job_start(_CONTEXT)

    assert raised_error.value is compensation_error
    compensation_request = jobs_dao.mark_start_failure_compensation.call_args.args[0]
    assert compensation_request.job_definition_arn == _JOB_DEFINITION_ARN
    batch_client.submit_job.assert_not_called()
    batch_client.terminate_job.assert_not_called()
    jobs_dao.update_job_status.assert_not_called()
    cleanup.cleanup.assert_not_called()


def test_definition_persistence_failure_replay_cleans_retained_exact_arn_after_failed_write() -> None:
    terminal_error = RuntimeError("failed persistence failed")
    events: list[str] = []
    stored_job = SimpleNamespace(
        status=JobStatus.STARTED,
        batch_job_id=None,
        batch_job_definition_arn=None,
        batch_job_queue=None,
        batch_job_name=None,
        start_orchestration_claimed_at=None,
        start_failure_compensation_started_at=None,
    )
    batch_client = MagicMock()
    batch_client.register_job_definition.return_value = {"jobDefinitionArn": _JOB_DEFINITION_ARN}
    jobs_dao = MagicMock()

    def claim_start_orchestration(_request: object) -> bool:
        if stored_job.start_orchestration_claimed_at is not None:
            return False
        stored_job.start_orchestration_claimed_at = 1.0
        return True

    def mark_start_failure_compensation(request: StartFailureCompensationRequest) -> None:
        events.append("compensation")
        assert request.batch_job_id is None
        assert request.job_definition_arn == _JOB_DEFINITION_ARN
        stored_job.start_failure_compensation_started_at = 2.0
        stored_job.batch_job_definition_arn = request.job_definition_arn

    def update_job_status(request: UpdateJobStatusRequest) -> None:
        events.append("status:FAILED")
        assert request.status is JobStatus.FAILED
        if events.count("status:FAILED") == 1:
            raise terminal_error
        stored_job.status = request.status

    cleanup = MagicMock()

    def cleanup_definition(request: BatchJobDefinitionRequest) -> None:
        events.append("cleanup")
        assert stored_job.status is JobStatus.FAILED
        assert request.job_definition_arn == _JOB_DEFINITION_ARN

    jobs_dao.claim_start_orchestration.side_effect = claim_start_orchestration
    jobs_dao.store_batch_job_definition.side_effect = RuntimeError("persistence failed")
    jobs_dao.mark_start_failure_compensation.side_effect = mark_start_failure_compensation
    jobs_dao.update_job_status.side_effect = update_job_status
    jobs_dao.get_job.side_effect = lambda _request: stored_job
    cleanup.cleanup.side_effect = cleanup_definition
    connectors_dao = MagicMock()
    connectors_dao.get_connector.return_value = stored_connector()

    with (
        patch.object(job_orchestrator_handler, "batch_client", batch_client),
        patch.object(job_orchestrator_handler, "jobs_dao", jobs_dao),
        patch.object(job_orchestrator_handler, "connectors_dao", connectors_dao),
        patch.object(job_orchestrator_handler, "batch_job_definition_cleanup", cleanup),
    ):
        with pytest.raises(RuntimeError) as raised_error:
            job_orchestrator_handler.handle_job_start(_CONTEXT)
        assert raised_error.value is terminal_error
        assert stored_job.batch_job_definition_arn == _JOB_DEFINITION_ARN
        cleanup.cleanup.assert_not_called()

        job_orchestrator_handler.handle_job_start(_CONTEXT)

    assert events == ["compensation", "status:FAILED", "status:FAILED", "cleanup"]
    batch_client.register_job_definition.assert_called_once()
    batch_client.submit_job.assert_not_called()
    batch_client.terminate_job.assert_not_called()
    jobs_dao.mark_start_failure_compensation.assert_called_once()
    assert stored_job.status is JobStatus.FAILED


def test_start_failure_compensation_replay_cleans_only_after_failed_persists() -> None:
    batch_client = MagicMock()
    batch_client.register_job_definition.return_value = {"jobDefinitionArn": _JOB_DEFINITION_ARN}
    batch_client.submit_job.side_effect = ClientError(
        {
            "Error": {"Code": "ClientException", "Message": "rejected"},
            "ResponseMetadata": {"HTTPStatusCode": 400},
        },
        "SubmitJob",
    )
    stored_job = SimpleNamespace(
        status=JobStatus.STARTED,
        batch_job_id=None,
        batch_job_definition_arn=None,
        batch_job_queue=None,
        batch_job_name=None,
        start_orchestration_claimed_at=None,
        start_failure_compensation_started_at=None,
    )
    jobs_dao = MagicMock()
    events: list[str] = []

    def claim_start_orchestration(_request: object) -> bool:
        if stored_job.start_orchestration_claimed_at is not None:
            return False
        stored_job.start_orchestration_claimed_at = 1.0
        return True

    def store_batch_job_definition(request: BatchJobDefinitionRequest) -> None:
        stored_job.batch_job_definition_arn = request.job_definition_arn

    def mark_start_failure_compensation(request: StartFailureCompensationRequest) -> None:
        events.append("compensation")
        stored_job.start_failure_compensation_started_at = 2.0
        stored_job.batch_job_id = request.batch_job_id

    def update_job_status(request: UpdateJobStatusRequest) -> None:
        events.append("status:FAILED")
        stored_job.status = request.status

    cleanup = MagicMock()

    def cleanup_definition(request: BatchJobDefinitionRequest) -> None:
        events.append("cleanup")
        assert stored_job.status is JobStatus.FAILED
        assert request.job_definition_arn == stored_job.batch_job_definition_arn
        if events.count("cleanup") == 1:
            raise RuntimeError("cleanup failed")
        stored_job.batch_job_definition_arn = None

    jobs_dao.claim_start_orchestration.side_effect = claim_start_orchestration
    jobs_dao.store_batch_job_definition.side_effect = store_batch_job_definition
    jobs_dao.mark_start_failure_compensation.side_effect = mark_start_failure_compensation
    jobs_dao.update_job_status.side_effect = update_job_status
    jobs_dao.get_job.side_effect = lambda _request: stored_job
    cleanup.cleanup.side_effect = cleanup_definition
    connectors_dao = MagicMock()
    connectors_dao.get_connector.return_value = stored_connector()

    with (
        patch.object(job_orchestrator_handler, "batch_client", batch_client),
        patch.object(job_orchestrator_handler, "jobs_dao", jobs_dao),
        patch.object(job_orchestrator_handler, "connectors_dao", connectors_dao),
        patch.object(job_orchestrator_handler, "batch_job_definition_cleanup", cleanup),
    ):
        with pytest.raises(RuntimeError, match="cleanup failed"):
            job_orchestrator_handler.handle_job_start(_CONTEXT)
        job_orchestrator_handler.handle_job_start(_CONTEXT)

    assert events == ["compensation", "status:FAILED", "cleanup", "cleanup"]
    batch_client.register_job_definition.assert_called_once()
    batch_client.submit_job.assert_called_once()
    batch_client.terminate_job.assert_not_called()
    jobs_dao.mark_start_failure_compensation.assert_called_once()
    jobs_dao.update_job_status.assert_called_once()
    assert stored_job.status is JobStatus.FAILED
    assert stored_job.batch_job_definition_arn is None


def test_compensation_replay_retries_termination_before_failed_and_cleanup() -> None:
    events: list[str] = []
    stored_job = SimpleNamespace(
        status=JobStatus.STARTED,
        batch_job_id="batch-1",
        batch_job_definition_arn=_JOB_DEFINITION_ARN,
        batch_job_queue=_JOB_QUEUE,
        batch_job_name="job-1",
        start_orchestration_claimed_at=1.0,
        start_failure_compensation_started_at=2.0,
    )
    jobs_dao = MagicMock()
    jobs_dao.claim_start_orchestration.return_value = False
    jobs_dao.get_job.return_value = stored_job
    jobs_dao.update_job_status.side_effect = lambda _request: events.append("status:FAILED")
    batch_client = MagicMock()
    batch_client.terminate_job.side_effect = lambda **_kwargs: events.append("terminate")
    cleanup = MagicMock()
    cleanup.cleanup.side_effect = lambda _request: events.append("cleanup")

    with (
        patch.object(job_orchestrator_handler, "batch_client", batch_client),
        patch.object(job_orchestrator_handler, "jobs_dao", jobs_dao),
        patch.object(job_orchestrator_handler, "batch_job_definition_cleanup", cleanup),
    ):
        job_orchestrator_handler.handle_job_start(_CONTEXT)

    assert events == ["terminate", "status:FAILED", "cleanup"]
    jobs_dao.mark_start_failure_compensation.assert_not_called()


def test_compensation_replay_termination_failure_retains_arn_and_started_status() -> None:
    termination_error = RuntimeError("termination failed")
    stored_job = SimpleNamespace(
        status=JobStatus.STARTED,
        batch_job_id="batch-1",
        batch_job_definition_arn=_JOB_DEFINITION_ARN,
        batch_job_queue=_JOB_QUEUE,
        batch_job_name="job-1",
        start_orchestration_claimed_at=1.0,
        start_failure_compensation_started_at=2.0,
    )
    jobs_dao = MagicMock()
    jobs_dao.claim_start_orchestration.return_value = False
    jobs_dao.get_job.return_value = stored_job
    batch_client = MagicMock()
    batch_client.terminate_job.side_effect = termination_error
    cleanup = MagicMock()

    with (
        patch.object(job_orchestrator_handler, "batch_client", batch_client),
        patch.object(job_orchestrator_handler, "jobs_dao", jobs_dao),
        patch.object(job_orchestrator_handler, "batch_job_definition_cleanup", cleanup),
        pytest.raises(RuntimeError) as raised_error,
    ):
        job_orchestrator_handler.handle_job_start(_CONTEXT)

    assert raised_error.value is termination_error
    assert stored_job.status is JobStatus.STARTED
    assert stored_job.batch_job_definition_arn == _JOB_DEFINITION_ARN
    jobs_dao.update_job_status.assert_not_called()
    cleanup.cleanup.assert_not_called()


def test_orchestrator_logs_exclude_sensitive_values_on_success_and_failure() -> None:
    connector_name = "private-connector-name-for-log-test"
    connector_description = "private-connector-description-for-log-test"
    secret_reference = "arn:aws:secretsmanager:us-west-2:123456789012:secret:private-reference"
    connector_configuration = f"encrypted:{secret_reference}".encode()
    job_configuration = b"private-job-configuration-for-log-test"
    source_external_id = "private-source-external-id-for-log-test"
    source_role_arn = "arn:aws:iam::210987654321:role/private-source-access"
    exception_message = "private-exception-message-for-log-test"
    connector = replace(
        stored_connector(),
        name=connector_name,
        description=connector_description,
        encrypted_configuration=connector_configuration,
        source_access=SourceAccessConfiguration(
            role_arn=source_role_arn,
            external_id=source_external_id,
        ),
    )
    batch_client = MagicMock()
    batch_client.register_job_definition.side_effect = [
        {"jobDefinitionArn": _JOB_DEFINITION_ARN},
        RuntimeError(exception_message),
    ]
    batch_client.submit_job.return_value = {"jobId": "batch-1"}
    jobs_dao = MagicMock()
    jobs_dao.claim_start_orchestration.return_value = True
    jobs_dao.get_job.return_value = SimpleNamespace(
        status=JobStatus.FAILED,
        encrypted_configuration=job_configuration,
        batch_job_definition_arn=_JOB_DEFINITION_ARN,
        start_failure_compensation_started_at=None,
    )
    connectors_dao = MagicMock()
    connectors_dao.get_connector.return_value = connector
    cleanup = MagicMock()
    logger = MagicMock()

    with (
        patch.object(job_orchestrator_handler, "batch_client", batch_client),
        patch.object(job_orchestrator_handler, "jobs_dao", jobs_dao),
        patch.object(job_orchestrator_handler, "connectors_dao", connectors_dao),
        patch.object(job_orchestrator_handler, "batch_job_definition_cleanup", cleanup),
        patch.object(job_orchestrator_handler, "logger", logger),
    ):
        job_orchestrator_handler.handle_job_start(_CONTEXT)
        jobs_dao.claim_start_orchestration.return_value = False
        job_orchestrator_handler.handle_job_start(_CONTEXT)
        jobs_dao.claim_start_orchestration.return_value = True
        job_orchestrator_handler.handle_job_start(_CONTEXT)

    logged_calls = repr(logger.mock_calls)
    for sensitive_value in (
        connector_name,
        connector_description,
        connector_configuration.decode(),
        job_configuration.decode(),
        source_external_id,
        source_role_arn,
        secret_reference,
        exception_message,
    ):
        assert sensitive_value not in logged_calls
