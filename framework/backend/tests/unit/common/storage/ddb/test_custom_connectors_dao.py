"""Custom connector records: the create transaction, reads, lists, updates, and deletion."""

import base64
import json
from typing import Any

import pytest
from ccf_api_models.identifiers import ConnectorRef, DestinationRef, JobRef, Partition, TenantContext
from ccf_api_models.models import (
    ConnectorSchedule,
    ConnectorStatus,
    LogDestination,
    LogDestinationType,
    ResourceRequirements,
    ScheduleState,
    SourceAccessInput,
    UpdateContainerProperties,
    UpdateCustomConnectorRequest,
)

from common.errors import BadRequestError, ResourceConflictError, ResourceNotFoundError
from common.storage.ddb.custom_connector_jobs_dao import StartJobRecord
from common.storage.ddb.custom_connectors_dao import (
    ConnectorChange,
    ConnectorItem,
    ConnectorUpdate,
    DeletionExecutionRecord,
)
from common.storage.ddb.model_table import ListItemsRequest

DESTINATION_ID = "dst-7c41e9a2b5d0"
CONNECTOR_ID = "cc-e020d65d76c0"
CONNECTOR = ConnectorRef(
    destination=DestinationRef(
        tenant=TenantContext(partition=Partition.AWS, region="us-east-1", account_id="111122223333"),
        destination_id=DESTINATION_ID,
    ),
    connector_id=CONNECTOR_ID,
)
DELETION_ARN = "arn:aws:states:us-east-1:444455556666:execution:CcfConnectorDeletionWorkflow:cc-e020d65d76c0-delete-1"
SCHEDULE = ConnectorSchedule(expression="rate(1 day)", state=ScheduleState.ENABLED)


def log_destination(world: Any) -> LogDestination:
    return LogDestination(type=LogDestinationType.CLOUDWATCH_LOGS, arn=world.log_group_arn, role_arn=world.log_role_arn)


def request(**members: Any) -> UpdateCustomConnectorRequest:
    return UpdateCustomConnectorRequest(destination_id=DESTINATION_ID, connector_id=CONNECTOR_ID, **members)


def merged(stored: ConnectorItem, payload: UpdateCustomConnectorRequest) -> ConnectorItem:
    return stored.with_update(ConnectorChange(payload=payload))


def update(world: Any, payload: UpdateCustomConnectorRequest) -> ConnectorItem:
    stored = world.connectors.get(CONNECTOR)
    updated: ConnectorItem = world.connectors.update(ConnectorUpdate(stored=stored, merged=merged(stored, payload)))
    return updated


def test_create_saves_an_available_connector_with_its_generated_values(world: Any) -> None:
    world.active_destination()

    stored = world.connectors.create(
        world.connector_record(world.ConnectorSeed(log_destination=log_destination(world)))
    )

    assert stored.status is ConnectorStatus.AVAILABLE
    assert stored.arn == (
        "arn:aws:ccf:us-east-1:111122223333:destination/dst-7c41e9a2b5d0/custom-connector/cc-e020d65d76c0"
    )
    assert stored.source_access.external_id == "ccf-ext-0123456789abcdef0123456789abcdef"
    assert stored.runtime_role_arn == world.runtime_role_arn
    assert stored.encrypted_configuration == b"ciphertext"
    assert world.connectors.get(world.connector).log_destination == log_destination(world)


@pytest.mark.parametrize("status", ["CREATING_BUCKET", "FAILED", "DELETING"])
def test_create_conflicts_and_writes_nothing_while_the_destination_is_not_active(world: Any, status: str) -> None:
    world.destinations.create(world.destination_record())
    world.set_destination_status(world.StatusChange(status=status))

    with pytest.raises(ResourceConflictError, match="Destination is not ACTIVE"):
        world.connectors.create(world.connector_record())

    with pytest.raises(ResourceNotFoundError):
        world.connectors.get(world.connector)


def test_create_conflicts_when_the_destination_does_not_exist(world: Any) -> None:
    with pytest.raises(ResourceConflictError):
        world.connectors.create(world.connector_record())


def test_create_given_an_existing_connector_id_then_conflicts_without_blaming_the_destination(world: Any) -> None:
    world.available_connector()

    with pytest.raises(ResourceConflictError) as raised:
        world.connectors.create(world.connector_record())

    assert "Destination is not ACTIVE" not in str(raised.value)


def test_create_stores_the_log_group_arn_without_its_streams_suffix(world: Any) -> None:
    world.active_destination()
    suffixed = log_destination(world).model_copy(update={"arn": f"{world.log_group_arn}:*"})

    stored = world.connectors.create(world.connector_record(world.ConnectorSeed(log_destination=suffixed)))

    assert stored.log_destination == log_destination(world)
    assert stored.log_group_name == "/ccf/connectors/github"


def test_get_of_a_connector_in_another_destination_is_not_found(world: Any) -> None:
    world.available_connector()

    with pytest.raises(ResourceNotFoundError):
        world.connectors.get(
            ConnectorRef(destination=world.other_destination, connector_id=world.connector.connector_id)
        )


def test_get_reads_with_strong_consistency(world: Any, consistent_read_spy: Any) -> None:
    world.available_connector()
    world, spy = world.spying_on(world.names.connectors)

    world.connectors.get(world.connector)

    assert spy.reads == [True]


def test_exists_given_a_stored_connector_then_true(world: Any) -> None:
    world.available_connector()

    assert world.connectors.exists(world.connector) is True


def test_exists_given_no_stored_connector_then_false(world: Any) -> None:
    assert world.connectors.exists(world.connector) is False


def test_list_returns_only_the_destinations_connectors_by_name_when_asked(world: Any) -> None:
    world.available_connector()
    world.connectors.create(
        world.connector_record(
            world.ConnectorSeed(
                connector=ConnectorRef(destination=world.destination, connector_id="cc-000000000001"), name="wiki"
            )
        )
    )
    world.active_destination(world.other_destination)
    world.connectors.create(
        world.connector_record(
            world.ConnectorSeed(
                connector=ConnectorRef(destination=world.other_destination, connector_id="cc-000000000002")
            )
        )
    )

    everything = world.connectors.list(ListItemsRequest(parent=world.destination, max_results=100))
    by_name = world.connectors.list(ListItemsRequest(parent=world.destination, max_results=100, filter_value="wiki"))

    assert sorted(summary.connector_id for summary in everything.items) == ["cc-000000000001", "cc-e020d65d76c0"]
    assert [summary.connector_id for summary in by_name.items] == ["cc-000000000001"]


def test_list_by_name_rejects_a_token_issued_for_another_name(world: Any) -> None:
    world.available_connector()
    prefix = f"{DESTINATION_ID}#"
    key = {
        "tenant_arn_prefix": world.tenant.arn_prefix,
        "destination_connector_key": f"{prefix}{CONNECTOR_ID}",
        "destination_name_key": f"{prefix}wiki",
    }
    token = base64.urlsafe_b64encode(json.dumps(key).encode()).decode()

    with pytest.raises(BadRequestError):
        world.connectors.list(
            ListItemsRequest(parent=world.destination, max_results=100, filter_value="docs", next_token=token)
        )


def test_has_connectors_reports_whether_any_remains_in_the_destination(world: Any) -> None:
    world.available_connector()

    assert world.connectors.has_connectors(world.destination) is True
    assert world.connectors.has_connectors(world.other_destination) is False


def test_update_replaces_sent_members_and_keeps_the_rest(world: Any) -> None:
    world.available_connector()

    updated = update(
        world,
        request(
            description="Reads awslabs",
            container_properties=UpdateContainerProperties(
                resource_requirements=ResourceRequirements(cpu=2.0, memory=4096)
            ),
            source_access=SourceAccessInput(role_arn=world.source_role_arn.replace("github", "github2")),
            log_destination=log_destination(world),
        ),
    )

    assert updated.description == "Reads awslabs"
    assert updated.container_properties.resource_requirements.cpu == 2.0
    assert updated.container_properties.timeout == 3600
    assert updated.source_access.role_arn.endswith("CcfSourceRole-github2")
    assert updated.source_access.external_id == "ccf-ext-0123456789abcdef0123456789abcdef"
    assert updated.log_destination == log_destination(world)
    assert updated.name == "github-awslabs"
    assert updated.version == 2
    assert world.connectors.get(world.connector) == updated


def test_update_given_null_log_destination_and_schedule_then_clears_both(world: Any) -> None:
    world.available_connector()
    update(world, request(log_destination=log_destination(world), schedule=SCHEDULE))

    cleared = update(world, request(log_destination=None, schedule=None))

    assert (cleared.log_destination, cleared.schedule) == (None, None)
    assert world.connectors.get(world.connector).log_destination is None


def test_with_update_given_members_left_out_then_keeps_the_log_destination_and_schedule(world: Any) -> None:
    world.available_connector()
    stored = update(world, request(log_destination=log_destination(world), schedule=SCHEDULE))

    kept = merged(stored, request(description="changed"))

    assert (kept.log_destination, kept.schedule) == (stored.log_destination, SCHEDULE)


def test_with_update_given_a_new_name_then_changes_the_name_key(world: Any) -> None:
    world.available_connector()

    renamed = merged(world.connectors.get(world.connector), request(name="wiki"))

    assert (renamed.name, renamed.destination_name_key) == ("wiki", f"{DESTINATION_ID}#wiki")


def test_update_given_a_change_between_the_read_and_the_write_then_conflicts(world: Any) -> None:
    world.available_connector()
    stale = world.connectors.get(world.connector)
    update(world, request(description="first"))

    with pytest.raises(ResourceConflictError):
        world.connectors.update(ConnectorUpdate(stored=stale, merged=merged(stale, request(description="second"))))

    assert world.connectors.get(world.connector).description == "first"


def test_update_conflicts_while_a_job_runs(world: Any) -> None:
    world.started_job()

    with pytest.raises(ResourceConflictError):
        update(world, request(description="changed"))


def test_begin_deletion_sets_deleting_and_records_the_execution(world: Any) -> None:
    world.available_connector()

    world.connectors.begin_deletion(world.connector)
    world.connectors.record_deletion_execution(
        DeletionExecutionRecord(connector=world.connector, execution_arn=DELETION_ARN)
    )

    stored = world.connectors.get(world.connector)
    assert stored.status is ConnectorStatus.DELETING
    assert stored.deletion_execution_arn == DELETION_ARN


def test_begin_deletion_conflicts_while_a_job_runs(world: Any) -> None:
    world.started_job()

    with pytest.raises(ResourceConflictError):
        world.connectors.begin_deletion(world.connector)


def test_update_conflicts_while_deleting(world: Any) -> None:
    world.available_connector()
    world.connectors.begin_deletion(world.connector)

    with pytest.raises(ResourceConflictError):
        update(world, request(description="changed"))


def test_a_second_job_conflicts_while_the_first_holds_the_connector(world: Any) -> None:
    world.started_job()
    second = JobRef(connector=world.connector, job_id="ccj-000000000001")

    with pytest.raises(ResourceConflictError):
        world.jobs.start_job(StartJobRecord(job=second))
