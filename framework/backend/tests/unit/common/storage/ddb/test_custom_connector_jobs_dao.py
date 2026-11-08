"""Job records: the start transaction and its undo, reads, the listing, the stop, and expiry."""

from typing import Any

import pytest
from ccf_api_models.identifiers import ConnectorRef, DestinationRef, JobRef, Partition, TenantContext
from ccf_api_models.models import ConnectorStatus, JobStatus

from common.errors import ResourceConflictError, ResourceNotFoundError
from common.storage.ddb.custom_connector_jobs_dao import ConnectorInUseError, StartJobRecord
from common.storage.ddb.model_table import ListItemsRequest

NEWER_JOB_ID = "ccj-000000000001"


def raw_item(world: Any) -> dict[str, Any]:
    item: dict[str, Any] = world.table(world.names.jobs).get_item(
        Key={"tenant_arn_prefix": world.tenant.arn_prefix, "job_id": world.job.job_id}
    )["Item"]
    return item


def stopped_job_and_a_newer_one(world: Any) -> JobRef:
    world.started_job()
    world.jobs.request_stop(world.job)
    world.jobs.finish_stopped(world.job)
    newer = JobRef(connector=world.connector, job_id=NEWER_JOB_ID)
    world.jobs.start_job(StartJobRecord(job=newer))
    return newer


def hold_connector_for_another_job(world: Any) -> None:
    world.table(world.names.connectors).update_item(
        Key={
            "tenant_arn_prefix": world.tenant.arn_prefix,
            "destination_connector_key": world.connector.destination_connector_key,
        },
        UpdateExpression="SET active_job_id = :other",
        ExpressionAttributeValues={":other": "ccj-111111111111"},
    )


def test_start_saves_a_started_job_and_sets_the_connector_in_use(world: Any) -> None:
    world.started_job()

    stored = world.jobs.get_job(world.job)
    assert stored.status is JobStatus.STARTED
    assert stored.arn == f"{world.connector.arn}/job/{world.job.job_id}"
    assert stored.encrypted_configuration == b"job-ciphertext"
    assert world.connectors.get(world.connector).status is ConnectorStatus.IN_USE


def test_start_without_a_configuration_stores_none(world: Any) -> None:
    world.available_connector()

    world.jobs.start_job(StartJobRecord(job=world.job))

    assert world.jobs.get_job(world.job).encrypted_configuration is None


def test_start_for_a_missing_custom_connector_is_not_found(world: Any) -> None:
    world.active_destination()

    with pytest.raises(ResourceNotFoundError):
        world.jobs.start_job(StartJobRecord(job=world.job))


def test_start_conflicts_and_writes_nothing_while_the_destination_is_not_active(world: Any) -> None:
    world.available_connector()
    world.set_destination_status(world.StatusChange(status="DELETING"))

    with pytest.raises(ResourceConflictError) as raised:
        world.jobs.start_job(StartJobRecord(job=world.job))

    assert type(raised.value) is ResourceConflictError
    with pytest.raises(ResourceNotFoundError):
        world.jobs.get_job(world.job)
    assert world.connectors.get(world.connector).status is ConnectorStatus.AVAILABLE


def test_start_conflicts_while_the_connector_is_deleting(world: Any) -> None:
    world.available_connector()
    world.connectors.begin_deletion(world.connector)

    with pytest.raises(ResourceConflictError) as raised:
        world.jobs.start_job(StartJobRecord(job=world.job))

    assert type(raised.value) is ResourceConflictError


def test_start_while_another_job_holds_the_connector_raises_connector_in_use(world: Any) -> None:
    world.started_job()

    with pytest.raises(ConnectorInUseError):
        world.jobs.start_job(StartJobRecord(job=JobRef(connector=world.connector, job_id="ccj-111111111111")))


def test_cancel_start_deletes_the_job_and_releases_the_connector(world: Any) -> None:
    world.started_job()

    world.jobs.cancel_start(world.job)

    with pytest.raises(ResourceNotFoundError):
        world.jobs.get_job(world.job)
    assert world.connectors.get(world.connector).status is ConnectorStatus.AVAILABLE


def test_a_job_not_yet_terminal_expires_after_the_active_record_retention(world: Any) -> None:
    world.started_job()
    created = raw_item(world)
    world.jobs.request_stop(world.job)
    stopping = raw_item(world)

    fourteen_days = 14 * 24 * 60 * 60
    assert int(created["ttl"]) - int(created["created_at"]) == fourteen_days
    assert int(stopping["ttl"]) - int(stopping["updated_at"]) == fourteen_days


def test_a_terminal_job_expires_after_the_record_retention(world: Any) -> None:
    world.started_job()
    world.jobs.request_stop(world.job)
    world.jobs.finish_stopped(world.job)
    terminal = raw_item(world)

    assert terminal["status"] == "STOPPED"
    assert int(terminal["ttl"]) - int(terminal["updated_at"]) == 7 * 24 * 60 * 60


def test_get_of_a_job_under_another_connector_is_not_found(world: Any) -> None:
    world.started_job()

    with pytest.raises(ResourceNotFoundError):
        world.jobs.get_job(
            JobRef(
                connector=ConnectorRef(destination=world.destination, connector_id="cc-000000000001"),
                job_id=world.job.job_id,
            )
        )


@pytest.mark.parametrize(
    "destination",
    [
        DestinationRef(
            tenant=TenantContext(partition=Partition.AWS, region="us-east-1", account_id="999988887777"),
            destination_id="dst-7c41e9a2b5d0",
        ),
        DestinationRef(
            tenant=TenantContext(partition=Partition.AWS, region="us-east-1", account_id="111122223333"),
            destination_id="dst-0a1b2c3d4e5f",
        ),
    ],
    ids=["another-tenant", "another-destination"],
)
def test_get_of_a_job_under_another_tenant_or_destination_is_not_found(world: Any, destination: DestinationRef) -> None:
    world.started_job()

    with pytest.raises(ResourceNotFoundError):
        world.jobs.get_job(
            JobRef(
                connector=ConnectorRef(destination=destination, connector_id=world.connector.connector_id),
                job_id=world.job.job_id,
            )
        )


def test_get_reads_with_strong_consistency(world: Any, consistent_read_spy: Any) -> None:
    world.started_job()
    world, spy = world.spying_on(world.names.jobs)

    world.jobs.get_job(world.job)

    assert spy.reads == [True]


def test_list_returns_the_connectors_jobs_newest_first(world: Any) -> None:
    newer = stopped_job_and_a_newer_one(world)

    page = world.jobs.list(ListItemsRequest(parent=world.connector, max_results=100))

    assert [job.job_id for job in page.items] == [newer.job_id, world.job.job_id]


def test_list_keeps_only_jobs_in_the_filter_status(world: Any) -> None:
    stopped_job_and_a_newer_one(world)

    page = world.jobs.list(ListItemsRequest(parent=world.connector, max_results=100, filter_value="STOPPED"))

    assert [job.job_id for job in page.items] == [world.job.job_id]


def test_list_pages_continue_where_the_last_one_ended(world: Any) -> None:
    newer = stopped_job_and_a_newer_one(world)

    first = world.jobs.list(ListItemsRequest(parent=world.connector, max_results=1))
    second = world.jobs.list(ListItemsRequest(parent=world.connector, max_results=1, next_token=first.next_token))

    assert [job.job_id for job in first.items + second.items] == [newer.job_id, world.job.job_id]


def test_request_stop_sets_stopping_and_returns_the_job(world: Any) -> None:
    world.started_job()

    stopping = world.jobs.request_stop(world.job)

    assert stopping.status is JobStatus.STOPPING
    assert stopping.stop_requested_at is not None
    assert world.jobs.get_job(world.job) == stopping


@pytest.mark.parametrize("terminal", ["SUCCEEDED", "FAILED", "STOPPED"])
def test_request_stop_of_a_job_that_ended_conflicts(world: Any, terminal: str) -> None:
    world.started_job()
    world.set_job_status(terminal)

    with pytest.raises(ResourceConflictError):
        world.jobs.request_stop(world.job)


def test_a_repeated_stop_writes_the_stop_request_again(world: Any) -> None:
    world.started_job()
    world.jobs.request_stop(world.job)
    world.table(world.names.jobs).update_item(
        Key={"tenant_arn_prefix": world.tenant.arn_prefix, "job_id": world.job.job_id},
        UpdateExpression="REMOVE stop_requested_at",
    )

    world.jobs.request_stop(world.job)

    assert "stop_requested_at" in raw_item(world)


def test_finish_stopped_sets_stopped_and_releases_the_connector(world: Any) -> None:
    world.started_job()
    world.jobs.request_stop(world.job)

    finished = world.jobs.finish_stopped(world.job)

    assert finished is True
    assert world.jobs.get_job(world.job).status is JobStatus.STOPPED
    assert world.connectors.get(world.connector).status is ConnectorStatus.AVAILABLE


def test_finish_stopped_of_a_job_that_already_ended_returns_false_and_changes_nothing(world: Any) -> None:
    world.started_job()
    world.set_job_status("SUCCEEDED")
    before = raw_item(world)

    finished = world.jobs.finish_stopped(world.job)

    assert finished is False
    assert raw_item(world) == before
    assert world.connectors.get(world.connector).status is ConnectorStatus.IN_USE


def test_finish_stopped_leaves_a_connector_another_job_holds_alone(world: Any) -> None:
    world.started_job()
    world.jobs.request_stop(world.job)
    hold_connector_for_another_job(world)

    finished = world.jobs.finish_stopped(world.job)

    assert finished is True
    assert world.jobs.get_job(world.job).status is JobStatus.STOPPED
    assert world.connectors.get(world.connector).status is ConnectorStatus.IN_USE
