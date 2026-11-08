"""The write gate reads the destination once and takes the custom connector from the request."""

from __future__ import annotations

import json
from typing import Any

import pytest
from ccf_api_models.models import BatchPutCustomConnectorDocumentsRequest, ConnectorStatus, DestinationStatus

from common.errors import ResourceConflictError
from common.operation import OperationRequest, RequestTrace
from common.resource_data_codec import ResourceDataCodec
from common.settings import TableNames
from common.storage.ddb.custom_connectors_dao import ConnectorItem
from common.validators.connector_write_gate import ConnectorWriteGate
from tests.unit.dao_world import DaoTables, DaoWorld

TRACE = RequestTrace(correlation_id="correlation-1", api_request_id="request-1")


@pytest.fixture
def world(dynamodb: Any, table_names: TableNames, codec: ResourceDataCodec) -> DaoWorld:
    return DaoWorld(DaoTables(resource=dynamodb, names=table_names, codec=codec))


def request(world: DaoWorld, connector: ConnectorItem) -> OperationRequest[BatchPutCustomConnectorDocumentsRequest]:
    body = {
        "destination_id": world.destination.destination_id,
        "connector_id": world.connector.connector_id,
        "documents": [{"document_id": "doc-be49ed8650bb1e19", "source_document_id": "x", "content_hash": "sha256:1"}],
    }
    payload = BatchPutCustomConnectorDocumentsRequest.model_validate_json(json.dumps(body))
    return OperationRequest(tenant=world.tenant, payload=payload, trace=TRACE, connector=connector)


def test_an_active_destination_with_an_available_connector_returns_the_destination(world: DaoWorld) -> None:
    world.available_connector()
    stored = world.connectors.get(world.connector)
    spying, spy = world.spying_on(world.names.connectors)

    destination = ConnectorWriteGate(spying.destinations).check(request(spying, stored))

    assert destination.destination_id == world.destination.destination_id
    assert spy.reads == []


@pytest.mark.parametrize("status", [status for status in DestinationStatus if status is not DestinationStatus.ACTIVE])
def test_a_destination_that_isnt_active_conflicts(world: DaoWorld, status: DestinationStatus) -> None:
    world.available_connector()
    world.set_destination_status(world.StatusChange(status=status.value))

    with pytest.raises(ResourceConflictError):
        ConnectorWriteGate(world.destinations).check(request(world, world.connectors.get(world.connector)))


def test_a_deleting_custom_connector_conflicts(world: DaoWorld) -> None:
    world.available_connector()
    world.table(world.names.connectors).update_item(
        Key={
            "tenant_arn_prefix": world.tenant.arn_prefix,
            "destination_connector_key": world.connector.destination_connector_key,
        },
        UpdateExpression="SET #status = :deleting",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":deleting": ConnectorStatus.DELETING.value},
    )
    deleting = world.connectors.get(world.connector)

    with pytest.raises(ResourceConflictError):
        ConnectorWriteGate(world.destinations).check(request(world, deleting))
