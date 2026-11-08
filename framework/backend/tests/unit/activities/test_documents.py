"""The batch document operations over the moto tables, with a recording bucket and metrics."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

import pytest
from ccf_api_models.identifiers import ConnectorRef, DestinationRef, TenantContext
from ccf_api_models.models import (
    BatchDeleteCustomConnectorDocumentsRequest,
    BatchPutCustomConnectorDocumentsRequest,
    ConnectorStatus,
    DocumentRecord,
)

from activities.documents import BatchDeleteCustomConnectorDocumentsActivity, BatchPutCustomConnectorDocumentsActivity
from common.errors import BadRequestError, DependencyUnavailableError, ResourceConflictError, ResourceNotFoundError
from common.observability import MetricDimensionName, MetricName
from common.operation import OperationRequest, RequestTrace
from common.storage.s3.destination_files_dao import DestinationFilesDao
from common.validators.connector_write_gate import ConnectorWriteGate
from common.validators.source_identifiers import CollectionIdentifierValidator, DocumentIdentifierValidator
from tests.unit.activities.conftest import ActivitySeed, ActivityWorld
from tests.unit.dao_world import DaoWorld

TRACE = RequestTrace(correlation_id="correlation-1", api_request_id="request-1")
WORKFLOW_ARN = "arn:aws:states:us-east-1:444455556666:stateMachine:CcfDestinationWorkflow"
CONNECTOR = DaoWorld.connector
SOURCE_COLLECTION_ID = "630587223"
COLLECTION_ID = "col-" + hashlib.sha256(SOURCE_COLLECTION_ID.encode()).hexdigest()[:16]
IDS = {"destination_id": CONNECTOR.destination_id, "connector_id": CONNECTOR.connector_id}


def document(source_document_id: str) -> DocumentRecord:
    return DocumentRecord(
        document_id="doc-" + hashlib.sha256(source_document_id.encode()).hexdigest()[:16],
        source_document_id=source_document_id,
        content_hash="sha256:9f2c",
    )


def put_request(documents: list[DocumentRecord], **members: Any) -> BatchPutCustomConnectorDocumentsRequest:
    return BatchPutCustomConnectorDocumentsRequest(**IDS, documents=documents, **members)


def delete_request(document_ids: list[str], **members: Any) -> BatchDeleteCustomConnectorDocumentsRequest:
    return BatchDeleteCustomConnectorDocumentsRequest.model_validate({**IDS, "document_ids": document_ids, **members})


class DocumentWorld:
    """The two document activities over the activity world, called with the item the tenant resolver reads."""

    def __init__(self, world: ActivityWorld) -> None:
        self.world = world
        daos = world.daos
        gate = ConnectorWriteGate(daos.destinations)
        self.put_activity = BatchPutCustomConnectorDocumentsActivity(
            gate,
            daos.collections,
            daos.documents,
            CollectionIdentifierValidator(),
            DocumentIdentifierValidator(),
            world.metrics,
        )
        self.delete_activity = BatchDeleteCustomConnectorDocumentsActivity(
            gate, daos.collections, daos.documents, DestinationFilesDao(world.tenant_roles, WORKFLOW_ARN), world.metrics
        )

    def request[PayloadT: (BatchPutCustomConnectorDocumentsRequest, BatchDeleteCustomConnectorDocumentsRequest)](
        self, payload: PayloadT, tenant: TenantContext = DaoWorld.tenant
    ) -> OperationRequest[PayloadT]:
        """The request as the router passes it: the resolver's read answers 404 for an outsider."""
        bare: OperationRequest[PayloadT] = OperationRequest(tenant=tenant, payload=payload, trace=TRACE)
        destination = DestinationRef(tenant=tenant, destination_id=payload.destination_id)
        connector = self.world.daos.connectors.get(
            ConnectorRef(destination=destination, connector_id=payload.connector_id)
        )
        return bare.model_copy(update={"connector": connector})

    def put(self, documents: list[DocumentRecord], **members: Any) -> None:
        self.put_activity.put(self.request(put_request(documents, **members)))

    def delete(self, document_ids: list[str], **members: Any) -> None:
        self.delete_activity.delete(self.request(delete_request(document_ids, **members)))

    def items(self, table: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = self.world.daos.table(table).scan()["Items"]
        return items

    def documents(self) -> list[dict[str, Any]]:
        return self.items(self.world.daos.names.documents)

    def collections(self) -> list[dict[str, Any]]:
        return self.items(self.world.daos.names.collections)

    def mark_deleting(self, collection_id: str) -> None:
        self.world.daos.table(self.world.daos.names.collections).put_item(
            Item={
                "connector_arn": CONNECTOR.arn,
                "collection_id": collection_id,
                "deleting_at": 1,
                "created_at": 1,
                "updated_at": 1,
            }
        )


@pytest.fixture
def world(activity_world: ActivityWorld) -> DocumentWorld:
    return DocumentWorld(activity_world)


def test_documents_without_a_collection_go_to_the_default_collection(world: DocumentWorld) -> None:
    world.put([document("README.md")])

    [stored] = world.documents()
    assert stored["collection_id"] == "col-default"
    [collection] = world.collections()
    assert collection["collection_id"] == "col-default" and "source_collection_id" not in collection


def test_batch_put_creates_the_collection_item_with_its_source_id(world: DocumentWorld) -> None:
    world.put([document("630587223/README.md")], collection_id=COLLECTION_ID, source_collection_id=SOURCE_COLLECTION_ID)

    [collection] = world.collections()
    assert (collection["collection_id"], collection["source_collection_id"]) == (COLLECTION_ID, SOURCE_COLLECTION_ID)


def test_a_put_into_a_marked_collection_conflicts_through_the_transaction(world: DocumentWorld) -> None:
    world.mark_deleting(COLLECTION_ID)

    with pytest.raises(ResourceConflictError):
        world.put(
            [document("630587223/README.md")], collection_id=COLLECTION_ID, source_collection_id=SOURCE_COLLECTION_ID
        )

    assert world.documents() == []


def test_a_delete_in_a_marked_collection_conflicts_before_any_file_call(world: DocumentWorld) -> None:
    world.mark_deleting(COLLECTION_ID)

    with pytest.raises(ResourceConflictError):
        world.delete([document("630587223/README.md").document_id], collection_id=COLLECTION_ID)

    assert world.world.s3.calls == []
    assert world.world.tenant_roles.requests == []


@pytest.mark.parametrize("operation", ["put", "delete"])
@pytest.mark.parametrize(
    "activity_seed",
    [ActivitySeed(connector_status=ConnectorStatus.DELETING)],
    ids=["connector-deleting"],
)
def test_a_write_while_the_custom_connector_is_deleting_conflicts_and_writes_nothing(
    world: DocumentWorld, operation: str
) -> None:
    calls: dict[str, Callable[[], None]] = {
        "put": lambda: world.put([document("README.md")]),
        "delete": lambda: world.delete([document("README.md").document_id]),
    }

    with pytest.raises(ResourceConflictError):
        calls[operation]()

    assert world.documents() == world.collections() == []
    assert world.world.s3.calls == []


@pytest.mark.parametrize("operation", ["put", "delete"])
@pytest.mark.parametrize("status", ["CREATING_BUCKET", "DELETING", "FAILED"])
def test_a_write_while_the_destination_isnt_active_conflicts_and_writes_nothing(
    world: DocumentWorld, operation: str, status: str
) -> None:
    world.world.daos.set_destination_status(DaoWorld.StatusChange(status=status))
    calls: dict[str, Callable[[], None]] = {
        "put": lambda: world.put([document("README.md")]),
        "delete": lambda: world.delete([document("README.md").document_id]),
    }

    with pytest.raises(ResourceConflictError):
        calls[operation]()

    assert world.documents() == world.collections() == []
    assert world.world.s3.calls == []


def test_batch_delete_in_an_unknown_collection_succeeds_and_creates_no_item(world: DocumentWorld) -> None:
    world.delete([document("README.md").document_id], collection_id=COLLECTION_ID)

    assert world.collections() == []


def test_batch_delete_removes_the_files_and_then_only_the_deleted_documents_records(world: DocumentWorld) -> None:
    kept, gone = document("630587223/LICENSE"), document("630587223/README.md")
    world.put([kept, gone], collection_id=COLLECTION_ID, source_collection_id=SOURCE_COLLECTION_ID)

    world.delete([gone.document_id], collection_id=COLLECTION_ID)

    prefix = f"connector_id={CONNECTOR.connector_id}/collection_id={COLLECTION_ID}/document_id={gone.document_id}/"
    assert [call.split(" ")[-1] for call in world.world.s3.calls] == [prefix]
    assert [item["document_id"] for item in world.documents()] == [kept.document_id]


def test_a_failed_file_delete_keeps_the_records(world: DocumentWorld) -> None:
    record = document("README.md")
    world.put([record])
    world.world.s3.keys = {
        f"connector_id={CONNECTOR.connector_id}/collection_id=col-default/document_id={record.document_id}/README.md"
    }
    world.world.s3.fail_deletes = True

    with pytest.raises(DependencyUnavailableError):
        world.delete([record.document_id])

    assert len(world.documents()) == 1


def test_unknown_document_ids_are_ignored(world: DocumentWorld) -> None:
    world.delete([document("never-stored").document_id])

    assert world.documents() == []


def test_a_successful_put_and_delete_record_their_counts_and_a_rejected_call_records_none(
    world: DocumentWorld,
) -> None:
    documents = [document(f"file-{number}") for number in range(3)]
    with pytest.raises(BadRequestError):
        world.put([documents[0].model_copy(update={"document_id": "doc-0000000000000000"})])
    assert world.world.metrics.records == []

    world.put(documents)
    world.delete([documents[0].document_id])

    connector_dimension = {MetricDimensionName.CONNECTOR_ID: CONNECTOR.connector_id}
    assert [(record.name, record.value, record.dimensions) for record in world.world.metrics.records] == [
        (MetricName.DOCUMENTS_RECORDED, 3, connector_dimension),
        (MetricName.DOCUMENTS_DELETED, 1, connector_dimension),
    ]


OUTSIDERS = [
    pytest.param(TenantContext.from_arn_prefix("arn:aws:ccf:us-east-1:999988887777"), IDS, id="another-tenant"),
    pytest.param(DaoWorld.tenant, {**IDS, "destination_id": "dst-0a1b2c3d4e5f"}, id="another-destination"),
]


@pytest.mark.parametrize("operation", ["put", "delete"])
@pytest.mark.parametrize(("tenant", "ids"), OUTSIDERS)
def test_a_document_write_from_another_tenant_or_destination_is_not_found_and_writes_nothing(
    world: DocumentWorld, operation: str, tenant: TenantContext, ids: dict[str, str]
) -> None:
    stored = document("630587223/README.md")
    world.put([stored], collection_id=COLLECTION_ID, source_collection_id=SOURCE_COLLECTION_ID)
    before = (world.documents(), world.collections())
    calls: dict[str, Callable[[], None]] = {
        "put": lambda: world.put_activity.put(
            world.request(
                BatchPutCustomConnectorDocumentsRequest(**ids, documents=[document("630587223/LICENSE")]), tenant
            )
        ),
        "delete": lambda: world.delete_activity.delete(
            world.request(
                BatchDeleteCustomConnectorDocumentsRequest.model_validate(
                    {**ids, "document_ids": [stored.document_id]}
                ),
                tenant,
            )
        ),
    }

    with pytest.raises(ResourceNotFoundError):
        calls[operation]()

    assert (world.documents(), world.collections()) == before
    assert world.world.s3.calls == []
