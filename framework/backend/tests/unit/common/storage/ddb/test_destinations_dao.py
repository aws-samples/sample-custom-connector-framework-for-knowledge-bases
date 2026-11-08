"""Destination records: create, read, list, deletion status, and the ingestion token."""

from typing import Any

import pytest
from ccf_api_models.identifiers import DestinationRef, TenantContext
from ccf_api_models.models import DestinationStatus, DestinationType

from common.errors import BadRequestError, ResourceConflictError, ResourceNotFoundError
from common.resource_ids import SystemClock
from common.storage.ddb.destinations_dao import (
    DestinationsDao,
    DestinationsTable,
    IngestionTokenRequest,
    WorkflowExecutionRecord,
)
from common.storage.ddb.model_table import ListItemsRequest

EXECUTION_ARN = "arn:aws:states:us-east-1:444455556666:execution:CcfDestinationWorkflow:dst-7c41e9a2b5d0-create-1"


class QueryRecorder:
    """Wraps a table and records the index each query reads, `None` for the table itself."""

    def __init__(self, table: Any) -> None:
        self._table = table
        self.indexes: list[str | None] = []

    def query(self, **arguments: Any) -> Any:
        self.indexes.append(arguments.get("IndexName"))
        return self._table.query(**arguments)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._table, name)


def listing(tenant: TenantContext, **members: Any) -> ListItemsRequest[TenantContext]:
    return ListItemsRequest(parent=tenant, max_results=members.pop("max_results", 100), **members)


def token(world: Any, value: str) -> IngestionTokenRequest:
    return IngestionTokenRequest(destination=world.destination, token=value)


def test_create_saves_a_creating_bucket_destination_with_access_control_off_by_default(world: Any) -> None:
    stored = world.destinations.create(world.destination_record())

    assert stored.status is DestinationStatus.CREATING_BUCKET
    assert stored.access_control_enabled is False
    assert stored.arn == "arn:aws:ccf:us-east-1:111122223333:destination/dst-7c41e9a2b5d0"
    assert stored.destination_resources.bucket_arn is None
    assert stored.correlation_id == "correlation-1"


def test_access_control_enabled_is_stored_and_returned(world: Any) -> None:
    world.destinations.create(world.destination_record(world.DestinationSeed(access_control_enabled=True)))

    assert world.destinations.get(world.destination).access_control_enabled is True


def test_create_of_an_existing_destination_id_conflicts(world: Any) -> None:
    world.destinations.create(world.destination_record())

    with pytest.raises(ResourceConflictError):
        world.destinations.create(world.destination_record())


def test_get_of_another_tenants_destination_is_not_found(world: Any) -> None:
    world.destinations.create(world.destination_record())

    with pytest.raises(ResourceNotFoundError):
        world.destinations.get(
            DestinationRef(tenant=world.other_tenant, destination_id=world.destination.destination_id)
        )


def test_get_of_a_missing_destination_is_not_found(world: Any) -> None:
    with pytest.raises(ResourceNotFoundError):
        world.destinations.get(world.destination)


def test_get_reads_with_strong_consistency(world: Any) -> None:
    world.destinations.create(world.destination_record())
    world, spy = world.spying_on(world.names.destinations)

    world.destinations.get(world.destination)

    assert spy.reads == [True]


def test_exists_is_true_for_a_stored_destination(world: Any) -> None:
    world.destinations.create(world.destination_record())

    assert world.destinations.exists(world.destination) is True


def test_exists_is_false_for_a_missing_destination(world: Any) -> None:
    assert world.destinations.exists(world.destination) is False


def test_the_api_view_holds_only_the_destinations_members(world: Any) -> None:
    stored = world.destinations.create(world.destination_record())

    assert "tenant_arn_prefix" not in stored.destination().model_dump()
    assert stored.created().destination_id == world.destination.destination_id


def test_record_workflow_execution_stores_the_execution_arn(world: Any) -> None:
    world.destinations.create(world.destination_record())

    world.destinations.record_workflow_execution(
        WorkflowExecutionRecord(destination=world.destination, execution_arn=EXECUTION_ARN)
    )

    assert world.destinations.get(world.destination).workflow_execution_arn == EXECUTION_ARN


def test_delete_unstarted_removes_a_creating_bucket_destination_without_an_execution(world: Any) -> None:
    world.destinations.create(world.destination_record())

    world.destinations.delete_unstarted(world.destination)

    assert world.destinations.exists(world.destination) is False


def test_delete_unstarted_keeps_a_destination_whose_workflow_started(world: Any) -> None:
    world.destinations.create(world.destination_record())
    world.destinations.record_workflow_execution(
        WorkflowExecutionRecord(destination=world.destination, execution_arn=EXECUTION_ARN)
    )

    with pytest.raises(ResourceConflictError):
        world.destinations.delete_unstarted(world.destination)

    assert world.destinations.get(world.destination).status is DestinationStatus.CREATING_BUCKET


@pytest.mark.parametrize("status", ["ACTIVE", "FAILED"])
def test_begin_deletion_sets_deleting_and_keeps_the_previous_status(world: Any, status: str) -> None:
    world.destinations.create(world.destination_record())
    world.set_destination_status(world.StatusChange(status=status))

    stored = world.destinations.begin_deletion(world.destination)

    assert stored.status is DestinationStatus.DELETING
    assert stored.previous_status is DestinationStatus(status)


@pytest.mark.parametrize("status", ["CREATING_BUCKET", "CREATING_KNOWLEDGE_BASE", "CREATING_DATA_SOURCE", "DELETING"])
def test_begin_deletion_conflicts_from_any_other_status_and_changes_nothing(world: Any, status: str) -> None:
    world.destinations.create(world.destination_record())
    world.set_destination_status(world.StatusChange(status=status))

    with pytest.raises(ResourceConflictError):
        world.destinations.begin_deletion(world.destination)

    assert world.destinations.get(world.destination).status is DestinationStatus(status)


def test_begin_deletion_of_a_missing_destination_is_not_found(world: Any) -> None:
    with pytest.raises(ResourceNotFoundError):
        world.destinations.begin_deletion(world.destination)


def test_restore_previous_status_returns_the_destination_to_active(world: Any) -> None:
    world.active_destination()
    world.destinations.begin_deletion(world.destination)

    world.destinations.restore_previous_status(world.destination)

    stored = world.destinations.get(world.destination)
    assert stored.status is DestinationStatus.ACTIVE
    assert stored.previous_status is None


def test_list_reads_only_the_callers_partition_and_pages_with_a_token(world: Any) -> None:
    for index in range(3):
        world.destinations.create(
            world.destination_record(
                world.DestinationSeed(
                    destination=DestinationRef(tenant=world.tenant, destination_id=f"dst-00000000000{index}")
                )
            )
        )
    world.destinations.create(
        world.destination_record(
            world.DestinationSeed(
                destination=DestinationRef(tenant=world.other_tenant, destination_id="dst-0000000000ff")
            )
        )
    )

    first = world.destinations.list(listing(world.tenant, max_results=2))
    second = world.destinations.list(listing(world.tenant, max_results=2, next_token=first.next_token))

    listed = [summary.destination_id for summary in first.items + second.items]
    assert listed == ["dst-000000000000", "dst-000000000001", "dst-000000000002"]
    assert first.items[0].type is DestinationType.BEDROCK


def test_list_by_name_reads_the_by_name_index(world: Any) -> None:
    world.destinations.create(world.destination_record(world.DestinationSeed(name="docs")))
    world.destinations.create(
        world.destination_record(world.DestinationSeed(destination=world.other_destination, name="wiki"))
    )
    table = QueryRecorder(world.table(world.names.destinations))
    destinations = DestinationsDao.over(
        DestinationsTable(table=table, client=world.resource.meta.client, clock=SystemClock())
    )

    page = destinations.list(listing(world.tenant, filter_value="wiki"))

    assert [summary.destination_id for summary in page.items] == [world.other_destination.destination_id]
    assert table.indexes == ["ByName"]


def test_list_without_a_name_reads_the_tenant_partition_of_the_table(world: Any) -> None:
    world.destinations.create(world.destination_record())
    table = QueryRecorder(world.table(world.names.destinations))
    destinations = DestinationsDao.over(
        DestinationsTable(table=table, client=world.resource.meta.client, clock=SystemClock())
    )

    destinations.list(listing(world.tenant))

    assert table.indexes == [None]


def test_list_rejects_another_tenants_token(world: Any) -> None:
    for destination_id in ("dst-000000000001", "dst-000000000002"):
        world.destinations.create(
            world.destination_record(
                world.DestinationSeed(
                    destination=DestinationRef(tenant=world.other_tenant, destination_id=destination_id)
                )
            )
        )
    other_page = world.destinations.list(listing(world.other_tenant, max_results=1))

    with pytest.raises(BadRequestError):
        world.destinations.list(listing(world.tenant, next_token=other_page.next_token))


def seed_token(world: Any, value: str) -> None:
    """Write a waiting token as the job and connector deletion workflows do."""
    world.table(world.names.destinations).update_item(
        Key={"tenant_arn_prefix": world.tenant.arn_prefix, "destination_id": world.destination.destination_id},
        UpdateExpression="SET ingestion_pending = :token",
        ExpressionAttributeValues={":token": value},
    )


def test_clear_ingestion_pending_keeps_a_token_that_doesnt_match(world: Any) -> None:
    world.active_destination()
    seed_token(world, "ing-00000001")

    assert world.destinations.clear_ingestion_pending(token(world, "ing-00000009")) is False
    assert world.destinations.get(world.destination).ingestion_pending == "ing-00000001"


def test_clear_ingestion_pending_removes_the_matching_token_with_no_status_name(world: Any) -> None:
    world.active_destination()
    seed_token(world, "ing-00000001")

    assert world.destinations.clear_ingestion_pending(token(world, "ing-00000001")) is True
    assert world.destinations.get(world.destination).ingestion_pending is None


def test_restore_ingestion_pending_writes_the_token_when_none_waits(world: Any) -> None:
    world.active_destination()

    world.destinations.restore_ingestion_pending(token(world, "ing-00000001"))

    assert world.destinations.get(world.destination).ingestion_pending == "ing-00000001"


def test_restore_ingestion_pending_keeps_a_waiting_token(world: Any) -> None:
    world.active_destination()
    seed_token(world, "ing-00000002")

    world.destinations.restore_ingestion_pending(token(world, "ing-00000001"))

    assert world.destinations.get(world.destination).ingestion_pending == "ing-00000002"


def test_a_token_clear_and_restore_leave_updated_at_as_it_was(world: Any) -> None:
    world.active_destination()
    seed_token(world, "ing-00000001")
    before = world.destinations.get(world.destination).updated_at

    world.destinations.clear_ingestion_pending(token(world, "ing-00000001"))
    world.destinations.restore_ingestion_pending(token(world, "ing-00000001"))

    assert world.destinations.get(world.destination).updated_at == before


def test_clear_ingestion_pending_of_a_deleted_destination_reports_no_token(world: Any) -> None:
    assert world.destinations.clear_ingestion_pending(token(world, "ing-00000001")) is False


def test_restore_ingestion_pending_of_a_deleted_destination_writes_nothing_and_raises_nothing(world: Any) -> None:
    world.destinations.restore_ingestion_pending(token(world, "ing-00000001"))

    assert world.destinations.exists(world.destination) is False
