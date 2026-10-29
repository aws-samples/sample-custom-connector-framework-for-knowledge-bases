"""Atomic job creation and Batch execution identity tests."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError
from ccf_api_models.models import ConnectorStatus, JobStatus

from common.storage.ddb.custom_connector_jobs_dao import (
    BatchJobDefinitionRequest,
    BatchTerminalStatus,
    ClaimStartOrchestrationRequest,
    CustomConnectorJobsDao,
    ReconcileBatchJobStatusRequest,
    StartFailureCompensationRequest,
    StartJobRequest,
    UpdateJobStatusRequest,
)
from common.storage.ddb.custom_connectors_dao import UpdateConnectorStatusRequest
from common.storage.errors import StorageConflictError, StorageOperationError
from common.tenant import TenantContext

_TENANT = TenantContext("123456789012", "us-east-1")


def client_error(code: str, cancellation_codes: list[str] | None = None) -> ClientError:
    response = {"Error": {"Code": code, "Message": "test failure"}}
    if cancellation_codes is not None:
        response["CancellationReasons"] = [{"Code": reason} for reason in cancellation_codes]
    return ClientError(response, "TransactWriteItems")


def create_dao() -> tuple[CustomConnectorJobsDao, MagicMock, MagicMock, MagicMock]:
    jobs_table = MagicMock()
    jobs_table.name = "jobs"
    connectors_dao = MagicMock()
    connectors_dao.table = SimpleNamespace(name="connectors")
    transaction_client = MagicMock()
    return (
        CustomConnectorJobsDao(jobs_table, connectors_dao, transaction_client),
        jobs_table,
        connectors_dao,
        transaction_client,
    )


def deserialize(values: dict) -> dict:
    deserializer = TypeDeserializer()
    return {name: deserializer.deserialize(value) for name, value in values.items()}


def test_start_job_atomically_reserves_connector_and_puts_ciphertext_job() -> None:
    dao, jobs_table, connectors_dao, transaction_client = create_dao()

    summary = dao.start_job(StartJobRequest(_TENANT, "connector-1", b"ciphertext", "job-1"))

    jobs_table.get_item.assert_not_called()
    transact_items = transaction_client.transact_write_items.call_args.kwargs["TransactItems"]
    assert len(transact_items) == 2
    connector_update = transact_items[0]["Update"]
    job_put = transact_items[1]["Put"]
    assert connector_update["TableName"] == "connectors"
    assert deserialize(connector_update["Key"]) == {
        "custom_connector_arn_prefix": _TENANT.get_arn_prefix(),
        "connector_id": "connector-1",
    }
    assert "#status = :available" in connector_update["ConditionExpression"]
    update_values = deserialize(connector_update["ExpressionAttributeValues"])
    assert update_values[":in_use"] == ConnectorStatus.IN_USE.value
    assert update_values[":job_id"] == "job-1"
    assert update_values[":one"] == 1

    assert job_put["TableName"] == "jobs"
    assert job_put["ConditionExpression"] == "attribute_not_exists(job_id)"
    job_item = deserialize(job_put["Item"])
    assert job_item["job_id"] == "job-1"
    assert job_item["connector_id"] == "connector-1"
    assert job_item["encrypted_configuration"] == b"ciphertext"
    assert job_item["version"] == 1
    assert job_item["status"] == JobStatus.STARTED.value
    assert summary.job_id == "job-1"
    connectors_dao.update_connector_status.assert_not_called()


def test_start_job_conditional_cancellation_maps_to_conflict() -> None:
    dao, jobs_table, connectors_dao, transaction_client = create_dao()
    transaction_client.transact_write_items.side_effect = client_error(
        "TransactionCanceledException",
        ["ConditionalCheckFailed", "None"],
    )

    with pytest.raises(StorageConflictError, match="reservation or job identity"):
        dao.start_job(StartJobRequest(_TENANT, "connector-1", b"ciphertext", "job-1"))

    connectors_dao.update_connector_status.assert_not_called()
    jobs_table.get_item.assert_not_called()


def test_non_idempotent_transaction_conflict_preserves_operation_error_without_read() -> None:
    dao, jobs_table, connectors_dao, transaction_client = create_dao()
    transaction_client.transact_write_items.side_effect = client_error(
        "TransactionCanceledException",
        ["TransactionConflict", "None"],
    )

    with pytest.raises(StorageOperationError, match="Job creation failed"):
        dao.start_job(StartJobRequest(_TENANT, "connector-1", b"ciphertext", "job-1"))

    connectors_dao.update_connector_status.assert_not_called()
    jobs_table.get_item.assert_not_called()


def test_claim_start_orchestration_writes_separate_internal_metadata() -> None:
    dao, jobs_table, _, _ = create_dao()

    claimed = dao.claim_start_orchestration(ClaimStartOrchestrationRequest(_TENANT, "connector-1", "job-1"))

    assert claimed is True
    write = jobs_table.update_item.call_args.kwargs
    assert "start_orchestration_claimed_at" in write["UpdateExpression"]
    assert "encrypted_configuration" not in write["UpdateExpression"]
    assert "#status = :started" in write["ConditionExpression"]
    assert "attribute_not_exists(start_orchestration_claimed_at)" in write["ConditionExpression"]


def test_mark_start_failure_compensation_persists_replay_state_and_execution_identity() -> None:
    dao, jobs_table, _, _ = create_dao()

    dao.mark_start_failure_compensation(
        StartFailureCompensationRequest(
            _TENANT,
            "connector-1",
            "job-1",
            "batch-1",
            "definition-arn",
        )
    )

    write = jobs_table.update_item.call_args.kwargs
    assert "start_failure_compensation_started_at" in write["UpdateExpression"]
    assert "batch_job_id = :batch_job_id" in write["UpdateExpression"]
    assert "batch_job_definition_arn = :definition_arn" in write["UpdateExpression"]
    assert "attribute_exists(start_orchestration_claimed_at)" in write["ConditionExpression"]
    assert "attribute_not_exists(batch_job_definition_arn)" in write["ConditionExpression"]
    assert write["ExpressionAttributeValues"][":batch_job_id"] == "batch-1"
    assert write["ExpressionAttributeValues"][":definition_arn"] == "definition-arn"


def test_mark_start_failure_compensation_refuses_different_definition_arn() -> None:
    dao, jobs_table, _, _ = create_dao()
    jobs_table.update_item.side_effect = client_error("ConditionalCheckFailedException")
    jobs_table.get_item.return_value = {
        "Item": {
            "job_id": "job-1",
            "connector_id": "connector-1",
            "status": JobStatus.STARTED.value,
            "start_orchestration_claimed_at": 1,
            "start_failure_compensation_started_at": 2,
            "batch_job_definition_arn": "different-definition-arn",
        }
    }

    with pytest.raises(StorageConflictError, match="before start failure compensation"):
        dao.mark_start_failure_compensation(
            StartFailureCompensationRequest(
                _TENANT,
                "connector-1",
                "job-1",
                job_definition_arn="definition-arn",
            )
        )


def test_duplicate_start_orchestration_claim_returns_false() -> None:
    dao, jobs_table, _, _ = create_dao()
    jobs_table.update_item.side_effect = client_error("ConditionalCheckFailedException")
    jobs_table.get_item.return_value = {
        "Item": {
            "job_id": "job-1",
            "connector_id": "connector-1",
            "status": JobStatus.STARTED.value,
            "start_orchestration_claimed_at": 1,
        }
    }

    claimed = dao.claim_start_orchestration(ClaimStartOrchestrationRequest(_TENANT, "connector-1", "job-1"))

    assert claimed is False


def test_conditional_claim_failure_without_duplicate_state_preserves_conflict() -> None:
    dao, jobs_table, _, _ = create_dao()
    jobs_table.update_item.side_effect = client_error("ConditionalCheckFailedException")
    jobs_table.get_item.return_value = {
        "Item": {
            "job_id": "job-1",
            "connector_id": "connector-1",
            "status": JobStatus.STARTED.value,
        }
    }

    with pytest.raises(StorageConflictError, match="changed during start-orchestration claim"):
        dao.claim_start_orchestration(ClaimStartOrchestrationRequest(_TENANT, "connector-1", "job-1"))


def test_stopping_job_with_failed_batch_execution_reconciles_to_stopped() -> None:
    dao, jobs_table, connectors_dao, _ = create_dao()
    jobs_table.get_item.return_value = {
        "Item": {
            "job_id": "job-1",
            "connector_id": "connector-1",
            "status": JobStatus.STOPPING.value,
            "batch_job_id": "batch-1",
            "version": 2,
        }
    }

    status = dao.reconcile_batch_job_status(
        ReconcileBatchJobStatusRequest(
            _TENANT,
            "connector-1",
            "job-1",
            "batch-1",
            BatchTerminalStatus.FAILED,
        )
    )

    assert status is JobStatus.STOPPED
    write = jobs_table.update_item.call_args.kwargs
    assert write["ExpressionAttributeValues"][":status"] == JobStatus.STOPPED.value
    connectors_dao.update_connector_status.assert_called_once()


def test_stale_started_event_after_status_advancement_is_skipped() -> None:
    dao, jobs_table, _, _ = create_dao()
    jobs_table.update_item.side_effect = client_error("ConditionalCheckFailedException")
    jobs_table.get_item.return_value = {
        "Item": {
            "job_id": "job-1",
            "connector_id": "connector-1",
            "status": JobStatus.STOPPING.value,
        }
    }

    claimed = dao.claim_start_orchestration(ClaimStartOrchestrationRequest(_TENANT, "connector-1", "job-1"))

    assert claimed is False


def test_stop_transition_cannot_overtake_claimed_start_orchestration() -> None:
    dao, jobs_table, connectors_dao, _ = create_dao()
    jobs_table.get_item.return_value = {
        "Item": {
            "job_id": "job-1",
            "connector_id": "connector-1",
            "status": JobStatus.STARTED.value,
            "version": 2,
            "start_orchestration_claimed_at": 1,
        }
    }
    jobs_table.update_item.side_effect = client_error("ConditionalCheckFailedException")

    with pytest.raises(StorageConflictError, match="changed during status transition"):
        dao.update_job_status(UpdateJobStatusRequest(_TENANT, "connector-1", "job-1", JobStatus.STOPPING))

    write = jobs_table.update_item.call_args.kwargs
    assert "attribute_not_exists(start_orchestration_claimed_at)" in write["ConditionExpression"]
    connectors_dao.update_connector_status.assert_not_called()


def test_terminal_transition_conditions_on_matching_stored_batch_execution() -> None:
    dao, jobs_table, connectors_dao, _ = create_dao()
    jobs_table.get_item.return_value = {
        "Item": {
            "job_id": "job-1",
            "connector_id": "connector-1",
            "status": JobStatus.RUNNING.value,
            "batch_job_id": "batch-1",
            "version": 2,
        }
    }

    dao.update_job_status(UpdateJobStatusRequest(_TENANT, "connector-1", "job-1", JobStatus.SUCCEEDED, "batch-1"))

    write = jobs_table.update_item.call_args.kwargs
    assert "batch_job_id = :batch_job_id" in write["ConditionExpression"]
    assert "batch_job_id = :batch_job_id" not in write["UpdateExpression"]
    assert write["ExpressionAttributeValues"][":batch_job_id"] == "batch-1"
    connectors_dao.update_connector_status.assert_called_once()


def test_terminal_transition_mismatched_batch_execution_does_not_transition_or_release() -> None:
    dao, jobs_table, connectors_dao, _ = create_dao()
    stored_item = {
        "job_id": "job-1",
        "connector_id": "connector-1",
        "status": JobStatus.RUNNING.value,
        "batch_job_id": "batch-1",
        "version": 2,
    }
    jobs_table.get_item.return_value = {"Item": stored_item}
    jobs_table.update_item.side_effect = client_error("ConditionalCheckFailedException")

    with pytest.raises(StorageConflictError, match="changed during status transition"):
        dao.update_job_status(
            UpdateJobStatusRequest(_TENANT, "connector-1", "job-1", JobStatus.SUCCEEDED, "batch-wrong")
        )

    write = jobs_table.update_item.call_args.kwargs
    assert "batch_job_id = :batch_job_id" in write["ConditionExpression"]
    connectors_dao.update_connector_status.assert_not_called()


def test_duplicate_terminal_mismatched_batch_execution_does_not_release() -> None:
    dao, jobs_table, connectors_dao, _ = create_dao()
    jobs_table.get_item.return_value = {
        "Item": {
            "job_id": "job-1",
            "connector_id": "connector-1",
            "status": JobStatus.SUCCEEDED.value,
            "batch_job_id": "batch-1",
            "version": 3,
        }
    }

    with pytest.raises(StorageConflictError, match="Batch execution does not match"):
        dao.update_job_status(
            UpdateJobStatusRequest(_TENANT, "connector-1", "job-1", JobStatus.SUCCEEDED, "batch-wrong")
        )

    jobs_table.update_item.assert_not_called()
    connectors_dao.update_connector_status.assert_not_called()


@pytest.mark.parametrize(
    "existing_status",
    [
        JobStatus.STARTED,
        JobStatus.RUNNING,
        JobStatus.STOPPED,
        JobStatus.SUCCEEDED,
        JobStatus.FAILED,
    ],
)
def test_idempotent_start_conflict_returns_matching_existing_job(
    existing_status: JobStatus,
) -> None:
    dao, jobs_table, _, transaction_client = create_dao()
    operation_order: list[str] = []

    def reject_transaction(**_kwargs: Any) -> None:
        operation_order.append("transaction")
        raise client_error(
            "TransactionCanceledException",
            ["ConditionalCheckFailed", "ConditionalCheckFailed"],
        )

    def read_winner(**kwargs: Any) -> dict[str, Any]:
        operation_order.append("consistent-read")
        assert kwargs["ConsistentRead"] is True
        return {
            "Item": {
                "job_id": "job-1",
                "connector_id": "connector-1",
                "status": existing_status.value,
                "created_at": 1,
                "updated_at": 2,
            }
        }

    transaction_client.transact_write_items.side_effect = reject_transaction
    jobs_table.get_item.side_effect = read_winner

    existing = dao.start_job(
        StartJobRequest(
            _TENANT,
            "connector-1",
            b"ciphertext",
            "job-1",
            "scheduled-occurrence",
        )
    )

    assert existing.job_id == "job-1"
    assert existing.status is existing_status
    assert operation_order == ["transaction", "consistent-read"]
    jobs_table.get_item.assert_called_once_with(
        Key={
            "custom_connector_arn_prefix": _TENANT.get_arn_prefix(),
            "job_id": "job-1",
        },
        ConsistentRead=True,
    )


def test_idempotent_transaction_conflict_returns_matching_existing_job() -> None:
    dao, jobs_table, _, transaction_client = create_dao()
    operation_order: list[str] = []

    def reject_transaction(**_kwargs: Any) -> None:
        operation_order.append("transaction")
        raise client_error(
            "TransactionCanceledException",
            ["TransactionConflict", "None"],
        )

    def read_winner(**kwargs: Any) -> dict[str, Any]:
        operation_order.append("consistent-read")
        assert kwargs["ConsistentRead"] is True
        return {
            "Item": {
                "job_id": "job-1",
                "connector_id": "connector-1",
                "status": JobStatus.STARTED.value,
                "created_at": 1,
                "updated_at": 2,
            }
        }

    transaction_client.transact_write_items.side_effect = reject_transaction
    jobs_table.get_item.side_effect = read_winner

    existing = dao.start_job(
        StartJobRequest(
            _TENANT,
            "connector-1",
            b"ciphertext",
            "job-1",
            "scheduled-occurrence",
        )
    )

    assert existing.job_id == "job-1"
    assert operation_order == ["transaction", "consistent-read"]
    jobs_table.get_item.assert_called_once_with(
        Key={
            "custom_connector_arn_prefix": _TENANT.get_arn_prefix(),
            "job_id": "job-1",
        },
        ConsistentRead=True,
    )


def test_idempotent_start_conflict_rejects_unrelated_existing_identity() -> None:
    dao, jobs_table, _, transaction_client = create_dao()
    transaction_client.transact_write_items.side_effect = client_error(
        "TransactionCanceledException",
        ["ConditionalCheckFailed", "ConditionalCheckFailed"],
    )
    jobs_table.get_item.return_value = {
        "Item": {
            "job_id": "job-1",
            "connector_id": "connector-other",
            "status": JobStatus.SUCCEEDED.value,
            "created_at": 1,
            "updated_at": 2,
        }
    }

    with pytest.raises(StorageConflictError, match="reservation or job identity"):
        dao.start_job(
            StartJobRequest(
                _TENANT,
                "connector-1",
                b"ciphertext",
                "job-1",
                "scheduled-occurrence",
            )
        )


@pytest.mark.parametrize(
    "read_response",
    [
        {},
        {
            "Item": {
                "job_id": "job-1",
                "connector_id": "connector-other",
                "status": JobStatus.SUCCEEDED.value,
                "created_at": 1,
                "updated_at": 2,
            }
        },
    ],
)
def test_idempotent_transaction_conflict_without_matching_winner_preserves_operation_error(
    read_response: dict[str, Any],
) -> None:
    dao, jobs_table, _, transaction_client = create_dao()
    transaction_client.transact_write_items.side_effect = client_error(
        "TransactionCanceledException",
        ["ConditionalCheckFailed", "TransactionConflict"],
    )
    jobs_table.get_item.return_value = read_response

    with pytest.raises(StorageOperationError, match="Job creation failed"):
        dao.start_job(
            StartJobRequest(
                _TENANT,
                "connector-1",
                b"ciphertext",
                "job-1",
                "scheduled-occurrence",
            )
        )

    jobs_table.get_item.assert_called_once_with(
        Key={
            "custom_connector_arn_prefix": _TENANT.get_arn_prefix(),
            "job_id": "job-1",
        },
        ConsistentRead=True,
    )


def test_store_batch_job_definition_conditions_on_started_job_identity() -> None:
    dao, jobs_table, _, _ = create_dao()
    request = BatchJobDefinitionRequest(_TENANT, "connector-1", "job-1", "definition-arn")

    dao.store_batch_job_definition(request)

    write = jobs_table.update_item.call_args.kwargs
    assert "batch_job_definition_arn = :definition_arn" in write["UpdateExpression"]
    assert "#status = :started" in write["ConditionExpression"]
    assert write["ExpressionAttributeValues"][":definition_arn"] == "definition-arn"


def test_clear_batch_job_definition_uses_exact_arn_condition() -> None:
    dao, jobs_table, _, _ = create_dao()
    request = BatchJobDefinitionRequest(_TENANT, "connector-1", "job-1", "definition-arn")

    dao.clear_batch_job_definition(request)

    write = jobs_table.update_item.call_args.kwargs
    assert "REMOVE batch_job_definition_arn" in write["UpdateExpression"]
    assert "batch_job_definition_arn = :definition_arn" in write["ConditionExpression"]


def test_clear_batch_job_definition_is_idempotent_when_already_cleared() -> None:
    dao, jobs_table, _, _ = create_dao()
    jobs_table.update_item.side_effect = client_error("ConditionalCheckFailedException")
    jobs_table.get_item.return_value = {
        "Item": {
            "job_id": "job-1",
            "connector_id": "connector-1",
            "status": JobStatus.SUCCEEDED.value,
        }
    }

    dao.clear_batch_job_definition(BatchJobDefinitionRequest(_TENANT, "connector-1", "job-1", "definition-arn"))


def test_clear_batch_job_definition_does_not_clear_newer_arn() -> None:
    dao, jobs_table, _, _ = create_dao()
    jobs_table.update_item.side_effect = client_error("ConditionalCheckFailedException")
    jobs_table.get_item.return_value = {
        "Item": {
            "job_id": "job-1",
            "connector_id": "connector-1",
            "status": JobStatus.SUCCEEDED.value,
            "batch_job_definition_arn": "newer-definition-arn",
        }
    }

    with pytest.raises(StorageConflictError, match="changed during cleanup"):
        dao.clear_batch_job_definition(BatchJobDefinitionRequest(_TENANT, "connector-1", "job-1", "definition-arn"))


def test_already_stopped_reconciliation_does_not_regress_or_release_new_reservation() -> None:
    dao, jobs_table, connectors_dao, _ = create_dao()
    jobs_table.get_item.return_value = {
        "Item": {
            "job_id": "job-1",
            "connector_id": "connector-1",
            "status": JobStatus.STOPPED.value,
            "batch_job_id": "batch-1",
            "version": 3,
        }
    }
    connectors_dao.update_connector_status.side_effect = StorageConflictError("Connector reservation changed")

    status = dao.reconcile_batch_job_status(
        ReconcileBatchJobStatusRequest(
            _TENANT,
            "connector-1",
            "job-1",
            "batch-1",
            BatchTerminalStatus.SUCCEEDED,
        )
    )

    assert status is JobStatus.STOPPED
    jobs_table.update_item.assert_not_called()
    release_request = connectors_dao.update_connector_status.call_args.args[0]
    assert release_request.job_id == "job-1"
    assert release_request.status is ConnectorStatus.AVAILABLE


def test_post_submit_failure_persists_execution_for_terminal_replay() -> None:
    dao, jobs_table, connectors_dao, _ = create_dao()
    stored_item: dict[str, Any] = {
        "job_id": "job-1",
        "connector_id": "connector-1",
        "status": JobStatus.STARTED.value,
        "version": 2,
    }
    jobs_table.get_item.side_effect = lambda **_kwargs: {"Item": stored_item}

    def persist_terminal_execution(**kwargs: Any) -> dict[str, Any]:
        assert "batch_job_id = :batch_job_id" in kwargs["UpdateExpression"]
        assert "attribute_not_exists(batch_job_id) OR batch_job_id = :batch_job_id" in kwargs["ConditionExpression"]
        values = kwargs["ExpressionAttributeValues"]
        stored_item["status"] = values[":status"]
        stored_item["batch_job_id"] = values[":batch_job_id"]
        stored_item["version"] = values[":new_version"]
        return {}

    jobs_table.update_item.side_effect = persist_terminal_execution
    connector_state = {"active_job_id": "job-new"}
    release_job_ids: list[str] = []

    def preserve_new_reservation(request: UpdateConnectorStatusRequest) -> None:
        release_job_ids.append(request.job_id)
        if connector_state["active_job_id"] != request.job_id:
            raise StorageConflictError("Connector reservation changed")
        connector_state.pop("active_job_id")

    connectors_dao.update_connector_status.side_effect = preserve_new_reservation

    dao.update_job_status(
        UpdateJobStatusRequest(
            _TENANT,
            "connector-1",
            "job-1",
            JobStatus.FAILED,
            "batch-1",
        )
    )
    replayed_status = dao.reconcile_batch_job_status(
        ReconcileBatchJobStatusRequest(
            _TENANT,
            "connector-1",
            "job-1",
            "batch-1",
            BatchTerminalStatus.FAILED,
        )
    )

    assert replayed_status is JobStatus.FAILED
    assert stored_item["status"] == JobStatus.FAILED.value
    assert stored_item["batch_job_id"] == "batch-1"
    assert jobs_table.update_item.call_count == 1
    assert release_job_ids == ["job-1", "job-1"]
    assert connector_state == {"active_job_id": "job-new"}
