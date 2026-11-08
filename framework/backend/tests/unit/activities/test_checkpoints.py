"""Checkpoint operations' API rules: identifiers, the byte limit, the deleting conflict, and tenant scoping.

The storage outcomes (replace, read back, delete, the running-job conflict) are the DAO's tests.
"""

from __future__ import annotations

from typing import Any

import pytest
from ccf_api_models.identifiers import CollectionIdentity, TenantContext
from ccf_api_models.models import (
    ConnectorStatus,
    DeleteCustomConnectorCheckpointRequest,
    GetCustomConnectorCheckpointRequest,
    PutCustomConnectorCheckpointRequest,
)
from ccf_api_models.operations import Operation
from pydantic import SecretStr

from activities.checkpoints import (
    DeleteCustomConnectorCheckpointActivity,
    GetCustomConnectorCheckpointActivity,
    PutCustomConnectorCheckpointActivity,
)
from common.errors import BadRequestError, ResourceConflictError, ResourceNotFoundError
from common.operation import OperationRequest, RequestTrace
from common.validators.source_identifiers import CollectionIdentifierValidator
from tests.unit.activities.conftest import ActivitySeed, ActivityWorld
from tests.unit.dao_world import DaoWorld

TRACE = RequestTrace(correlation_id="correlation-1", api_request_id="request-1")
SOURCE_COLLECTION_ID = "630587223"
COLLECTION_ID = CollectionIdentity.of(SOURCE_COLLECTION_ID).collection_id
IDS = {"destination_id": DaoWorld.destination.destination_id, "connector_id": DaoWorld.connector.connector_id}


class CheckpointWorld:
    def __init__(self, world: ActivityWorld) -> None:
        collections = world.daos.collections
        self.daos = world.daos
        self.put_activity = PutCustomConnectorCheckpointActivity(collections, CollectionIdentifierValidator())
        self.get_activity = GetCustomConnectorCheckpointActivity(collections)
        self.delete_activity = DeleteCustomConnectorCheckpointActivity(collections)

    def put(self, payload: PutCustomConnectorCheckpointRequest) -> None:
        self.put_activity.put(OperationRequest(tenant=DaoWorld.tenant, payload=payload, trace=TRACE))

    def collection_items(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = self.daos.table(self.daos.names.collections).scan()["Items"]
        return items


@pytest.fixture
def world(activity_world: ActivityWorld) -> CheckpointWorld:
    return CheckpointWorld(activity_world)


@pytest.mark.parametrize(
    "payload",
    [
        PutCustomConnectorCheckpointRequest(
            **IDS, checkpoint_data=SecretStr("page-42"), collection_id=COLLECTION_ID, source_collection_id="another"
        ),
        PutCustomConnectorCheckpointRequest(**IDS, checkpoint_data=SecretStr("page-42"), collection_id=COLLECTION_ID),
        PutCustomConnectorCheckpointRequest(
            **IDS, checkpoint_data=SecretStr("page-42"), source_collection_id=SOURCE_COLLECTION_ID
        ),
        PutCustomConnectorCheckpointRequest(
            **IDS,
            checkpoint_data=SecretStr("page-42"),
            collection_id="col-default",
            source_collection_id=SOURCE_COLLECTION_ID,
        ),
    ],
)
def test_checkpoint_identifiers_that_dont_match_are_rejected_without_writing(
    world: CheckpointWorld, payload: PutCustomConnectorCheckpointRequest
) -> None:
    with pytest.raises(BadRequestError) as raised:
        world.put(payload)

    assert raised.value.fields == ("source_collection_id",)
    assert world.collection_items() == []


def test_a_checkpoint_over_16_kb_in_utf8_bytes_is_rejected(world: CheckpointWorld) -> None:
    with pytest.raises(BadRequestError) as raised:
        world.put(PutCustomConnectorCheckpointRequest(**IDS, checkpoint_data=SecretStr("€" * 5_462)))

    assert raised.value.fields == ("checkpoint_data",)


@pytest.mark.parametrize("activity_seed", [ActivitySeed(connector_status=ConnectorStatus.DELETING)])
def test_a_checkpoint_put_on_a_deleting_custom_connector_conflicts_and_writes_no_collection(
    world: CheckpointWorld,
) -> None:
    with pytest.raises(ResourceConflictError):
        world.put(PutCustomConnectorCheckpointRequest(**IDS, checkpoint_data=SecretStr("page-42")))

    assert world.collection_items() == []


OUTSIDERS = [
    pytest.param(DaoWorld.other_tenant, DaoWorld.destination.destination_id, id="another-tenant"),
    pytest.param(DaoWorld.tenant, DaoWorld.other_destination.destination_id, id="another-destination"),
]


@pytest.mark.parametrize(("tenant", "destination_id"), OUTSIDERS)
@pytest.mark.parametrize(
    "operation",
    [
        Operation.PUT_CUSTOM_CONNECTOR_CHECKPOINT,
        Operation.GET_CUSTOM_CONNECTOR_CHECKPOINT,
        Operation.DELETE_CUSTOM_CONNECTOR_CHECKPOINT,
    ],
)
def test_a_checkpoint_operation_from_another_tenant_or_destination_reaches_nothing(
    world: CheckpointWorld, tenant: TenantContext, destination_id: str, operation: Operation
) -> None:
    world.put(PutCustomConnectorCheckpointRequest(**IDS, checkpoint_data=SecretStr("page-42")))
    before = world.collection_items()
    ids = {"destination_id": destination_id, "connector_id": DaoWorld.connector.connector_id}

    match operation:
        case Operation.PUT_CUSTOM_CONNECTOR_CHECKPOINT:
            with pytest.raises(ResourceNotFoundError):
                world.put_activity.put(
                    OperationRequest(
                        tenant=tenant,
                        payload=PutCustomConnectorCheckpointRequest(**ids, checkpoint_data=SecretStr("page-43")),
                        trace=TRACE,
                    )
                )
        case Operation.GET_CUSTOM_CONNECTOR_CHECKPOINT:
            response = world.get_activity.fetch(
                OperationRequest(tenant=tenant, payload=GetCustomConnectorCheckpointRequest(**ids), trace=TRACE)
            )
            assert response.checkpoint is None
        case _:
            with pytest.raises(ResourceNotFoundError):
                world.delete_activity.delete(
                    OperationRequest(tenant=tenant, payload=DeleteCustomConnectorCheckpointRequest(**ids), trace=TRACE)
                )

    assert world.collection_items() == before
