"""The batch collection delete over the moto tables: only empty collections go, all or none."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest
from botocore.exceptions import ClientError
from ccf_api_models.catalog import OperationCatalog
from ccf_api_models.identifiers import CollectionRef, ConnectorRef, TenantContext
from ccf_api_models.models import (
    BatchDeleteCustomConnectorCollectionsRequest,
    DocumentRecord,
    ListCustomConnectorCollectionsRequest,
)
from ccf_api_models.operations import Operation

from activities.collections import BatchDeleteCustomConnectorCollectionsActivity
from common.errors import DependencyUnavailableError, ResourceConflictError, ResourceNotFoundError
from common.observability import MetricDimensionName, MetricName
from common.operation import OperationRequest, RequestTrace
from common.read_resources import ListResources
from common.resource_data_codec import ResourceDataCodec
from common.resource_ids import SystemClock
from common.storage.ddb.collections_dao import (
    CollectionsDao,
    CollectionsTable,
    CollectionWriteCondition,
    UpsertCollectionRequest,
    WriteGuardRequest,
)
from common.storage.ddb.documents_dao import PutDocumentsRecord
from tests.unit.activities.conftest import ActivityWorld
from tests.unit.dao_world import DaoWorld

TRACE = RequestTrace(correlation_id="correlation-1", api_request_id="request-1")
CONNECTOR = DaoWorld.connector
IDS = {"destination_id": CONNECTOR.destination_id, "connector_id": CONNECTOR.connector_id}
SOURCE_COLLECTION_ID = "630587223"
COLLECTION = CollectionRef(
    connector=CONNECTOR, collection_id="col-" + hashlib.sha256(SOURCE_COLLECTION_ID.encode()).hexdigest()[:16]
)
FIRST = CollectionRef(connector=CONNECTOR, collection_id="col-0000000000000001")


class FailingTable:
    """The collections table, failing one call for one collection with a DynamoDB server error."""

    def __init__(self, table: Any, call: str, collection_id: str) -> None:
        self._table = table
        self._call = call
        self._collection_id = collection_id

    def __getattr__(self, name: str) -> Any:
        method = getattr(self._table, name)
        if name != self._call:
            return method

        def failing(**arguments: Any) -> Any:
            if arguments["Key"]["collection_id"] == self._collection_id:
                error = {"Error": {"Code": "InternalServerError", "Message": "injected"}}
                raise ClientError(error, name)  # type: ignore[arg-type]
            return method(**arguments)

        return failing


class CollectionWorld:
    """The collection delete over the activity world; `failing` builds it over a failing collections table."""

    def __init__(self, world: ActivityWorld, codec: ResourceDataCodec) -> None:
        self.world = world
        self.codec = codec
        self.daos = world.daos

    def activity(self, table: Any | None = None) -> BatchDeleteCustomConnectorCollectionsActivity:
        collections = self.daos.collections
        if table is not None:
            collections = CollectionsDao.over(
                CollectionsTable(
                    table=table,
                    connectors_table=self.daos.table(self.daos.names.connectors),
                    client=self.daos.resource.meta.client,
                    codec=self.codec,
                    clock=SystemClock(),
                )
            )
        return BatchDeleteCustomConnectorCollectionsActivity(collections, self.daos.documents, self.world.metrics)

    def delete(self, collection_ids: list[str], table: Any | None = None) -> None:
        payload = BatchDeleteCustomConnectorCollectionsRequest.model_validate(
            {**IDS, "collections": [{"collection_id": collection_id} for collection_id in collection_ids]}
        )
        self.activity(table).delete(OperationRequest(tenant=DaoWorld.tenant, payload=payload, trace=TRACE))

    def failing(self, call: str) -> FailingTable:
        return FailingTable(self.daos.table(self.daos.names.collections), call, COLLECTION.collection_id)

    def items(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = self.daos.table(self.daos.names.collections).scan()["Items"]
        return items

    def upsert(self, collection: CollectionRef) -> None:
        self.daos.collections.upsert(
            UpsertCollectionRequest(collection=collection, source_collection_id=collection.collection_id)
        )


@pytest.fixture
def world(activity_world: ActivityWorld, codec: ResourceDataCodec) -> CollectionWorld:
    return CollectionWorld(activity_world, codec)


def test_the_listing_returns_source_collection_ids_and_none_for_the_default_collection(world: CollectionWorld) -> None:
    world.daos.collections.upsert(
        UpsertCollectionRequest(collection=CollectionRef(connector=CONNECTOR, collection_id="col-default"))
    )
    world.daos.collections.upsert(
        UpsertCollectionRequest(collection=COLLECTION, source_collection_id=SOURCE_COLLECTION_ID)
    )
    listing = ListResources(
        world.daos.collections.list,
        ConnectorRef.of,
        OperationCatalog.load().spec(Operation.LIST_CUSTOM_CONNECTOR_COLLECTIONS),
        world.daos.connectors.exists,
    )

    listed = listing.list(
        OperationRequest(
            tenant=DaoWorld.tenant,
            payload=ListCustomConnectorCollectionsRequest(
                destination_id=CONNECTOR.destination_id, connector_id=CONNECTOR.connector_id
            ),
            trace=TRACE,
        )
    )

    assert {(item.collection_id, item.source_collection_id) for item in listed.collections} == {  # type: ignore[attr-defined]
        ("col-default", None),
        (COLLECTION.collection_id, SOURCE_COLLECTION_ID),
    }


def test_an_empty_collection_is_deleted_an_unknown_one_ignored_and_the_count_recorded(world: CollectionWorld) -> None:
    world.upsert(COLLECTION)

    world.delete([COLLECTION.collection_id, "col-0000000000000000"])

    assert world.items() == []
    [record] = world.world.metrics.records
    assert record.name is MetricName.COLLECTIONS_DELETED
    assert record.dimensions == {MetricDimensionName.CONNECTOR_ID: CONNECTOR.connector_id}


def test_a_collection_with_documents_conflicts_and_every_mark_is_cleared(world: CollectionWorld) -> None:
    world.upsert(FIRST)
    world.upsert(COLLECTION)
    source_document_id = "630587223/README.md"
    world.daos.documents.put_documents(
        PutDocumentsRecord(
            collection=COLLECTION,
            documents=(
                DocumentRecord(
                    document_id="doc-" + hashlib.sha256(source_document_id.encode()).hexdigest()[:16],
                    source_document_id=source_document_id,
                    content_hash="sha256:9f2c",
                ),
            ),
            guard=world.daos.collections.write_guard(
                WriteGuardRequest(collection=COLLECTION, condition=CollectionWriteCondition.EXISTS_AND_NOT_DELETING)
            ),
        )
    )

    with pytest.raises(ResourceConflictError):
        world.delete([FIRST.collection_id, COLLECTION.collection_id])

    assert len(world.items()) == 2
    assert all("deleting_at" not in item for item in world.items())
    assert world.world.metrics.records == []


def test_a_failing_second_mark_clears_the_first_and_raises_the_failure(world: CollectionWorld) -> None:
    world.upsert(FIRST)
    world.upsert(COLLECTION)

    with pytest.raises(DependencyUnavailableError):
        world.delete([FIRST.collection_id, COLLECTION.collection_id], world.failing("update_item"))

    assert len(world.items()) == 2
    assert all("deleting_at" not in item for item in world.items())


def test_a_failing_delete_unmarks_without_recreating_a_deleted_collection(world: CollectionWorld) -> None:
    world.upsert(FIRST)
    world.upsert(COLLECTION)

    with pytest.raises(DependencyUnavailableError):
        world.delete([FIRST.collection_id, COLLECTION.collection_id], world.failing("delete_item"))

    [remaining] = world.items()
    assert remaining["collection_id"] == COLLECTION.collection_id
    assert "deleting_at" not in remaining


@pytest.mark.parametrize(
    "ids",
    [
        pytest.param({**IDS, "destination_id": "dst-0a1b2c3d4e5f"}, id="another-destination"),
        pytest.param({**IDS, "connector_id": "cc-000000000000"}, id="another-connector"),
    ],
)
def test_a_collection_delete_outside_the_custom_connector_deletes_nothing(
    world: CollectionWorld, ids: dict[str, str]
) -> None:
    world.upsert(COLLECTION)
    before = world.items()
    payload = BatchDeleteCustomConnectorCollectionsRequest.model_validate(
        {**ids, "collections": [{"collection_id": COLLECTION.collection_id}]}
    )
    request: OperationRequest[BatchDeleteCustomConnectorCollectionsRequest] = OperationRequest(
        tenant=DaoWorld.tenant, payload=payload, trace=TRACE
    )

    with pytest.raises(ResourceNotFoundError):
        world.daos.connectors.get(ConnectorRef.of(request))
    world.activity().delete(request)

    assert world.items() == before


def test_another_tenants_collection_delete_deletes_nothing(world: CollectionWorld) -> None:
    world.upsert(COLLECTION)
    before = world.items()
    payload = BatchDeleteCustomConnectorCollectionsRequest.model_validate(
        {**IDS, "collections": [{"collection_id": COLLECTION.collection_id}]}
    )

    world.activity().delete(
        OperationRequest(
            tenant=TenantContext.from_arn_prefix("arn:aws:ccf:us-east-1:999988887777"), payload=payload, trace=TRACE
        )
    )

    assert world.items() == before
