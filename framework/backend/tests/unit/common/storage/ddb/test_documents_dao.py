"""Document batches in one transaction guarded by their collection item."""

from typing import Any

import pytest
from annotated_types import MaxLen
from ccf_api_models.identifiers import CollectionRef
from ccf_api_models.models import BatchPutCustomConnectorDocumentsRequest, DocumentRecord, StoredDocument

from common.errors import ResourceConflictError
from common.storage.ddb.collections_dao import CollectionWriteCondition, UpsertCollectionRequest, WriteGuardRequest
from common.storage.ddb.documents_dao import DeleteDocumentsRecord, PutDocumentsRecord
from common.storage.ddb.model_table import ListItemsRequest

BATCH_LIMIT = next(
    item.max_length
    for item in BatchPutCustomConnectorDocumentsRequest.model_fields["documents"].metadata
    if isinstance(item, MaxLen)
)


def document(index: int, **members: Any) -> DocumentRecord:
    values = {
        "document_id": f"doc-{index:016x}",
        "source_document_id": f"docs/{index}.md",
        "content_hash": "sha256:abc",
    }
    return DocumentRecord.model_validate({**values, **members})


def put(world: Any, collection: CollectionRef, *documents: DocumentRecord) -> None:
    guard = world.collections.write_guard(
        WriteGuardRequest(collection=collection, condition=CollectionWriteCondition.EXISTS_AND_NOT_DELETING)
    )
    world.documents.put_documents(PutDocumentsRecord(collection=collection, documents=documents, guard=guard))


def delete(world: Any, collection: CollectionRef, *document_ids: str) -> None:
    guard = world.collections.write_guard(
        WriteGuardRequest(collection=collection, condition=CollectionWriteCondition.NOT_DELETING)
    )
    world.documents.delete_documents(
        DeleteDocumentsRecord(collection=collection, document_ids=document_ids, guard=guard)
    )


def stored(world: Any, collection_id: str | None = None) -> list[StoredDocument]:
    page = world.documents.list(ListItemsRequest(parent=world.connector, max_results=100, filter_value=collection_id))
    items: list[StoredDocument] = page.items
    return items


def upserted(world: Any, collection_id: str = "col-3f9a1c2b5d7e9f01") -> CollectionRef:
    target: CollectionRef = world.collection(collection_id)
    source = None if collection_id == "col-default" else "awslabs/mcp"
    world.collections.upsert(UpsertCollectionRequest(collection=target, source_collection_id=source))
    return target


def test_batch_put_records_the_model_batch_limit_in_one_transaction(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = upserted(world)
    client = world.resource.meta.client
    transactions: list[int] = []
    send = client.transact_write_items

    def counting_send(**arguments: Any) -> Any:
        transactions.append(len(arguments["TransactItems"]))
        return send(**arguments)

    monkeypatch.setattr(client, "transact_write_items", counting_send)

    put(world, target, *(document(index) for index in range(BATCH_LIMIT)))

    assert transactions == [BATCH_LIMIT + 1]
    assert len(stored(world)) == BATCH_LIMIT


def test_a_batch_whose_guard_fails_conflicts_and_writes_nothing(world: Any) -> None:
    with pytest.raises(ResourceConflictError):
        put(world, world.collection(), document(1))

    assert stored(world) == []


def test_batch_put_and_delete_into_a_deleting_collection_conflict(world: Any) -> None:
    target = upserted(world)
    put(world, target, document(1))
    world.collections.mark_deleting(target)

    with pytest.raises(ResourceConflictError):
        put(world, target, document(2))
    with pytest.raises(ResourceConflictError):
        delete(world, target, document(1).document_id)

    assert len(stored(world)) == 1


def test_batch_delete_in_an_unknown_collection_succeeds_and_creates_no_item(world: Any) -> None:
    delete(world, world.collection(), document(1).document_id)

    assert world.table(world.names.collections).scan()["Items"] == []


def test_rewriting_a_document_keeps_its_first_created_at(world: Any) -> None:
    target = upserted(world)
    put(world, target, document(1))
    [first] = stored(world)

    put(world, target, document(1, content_hash="sha256:def"))

    [second] = stored(world)
    assert (second.created_at, second.content_hash) == (first.created_at, "sha256:def")


def test_a_record_without_a_source_version_removes_the_stored_one(world: Any) -> None:
    target = upserted(world)
    put(world, target, document(1, source_version="v1"))

    put(world, target, document(1))

    [rewritten] = stored(world)
    assert rewritten.source_version is None


def test_list_with_a_collection_returns_only_that_collections_documents(world: Any) -> None:
    for collection_id in ("col-3f9a1c2b5d7e9f01", "col-default"):
        put(world, upserted(world, collection_id), document(1))

    assert [item.collection_id for item in stored(world, "col-default")] == ["col-default"]


def test_batch_delete_removes_the_records(world: Any) -> None:
    target = upserted(world)
    put(world, target, document(1), document(2))

    delete(world, target, document(1).document_id, "doc-ffffffffffffffff")

    assert [item.document_id for item in stored(world)] == [document(2).document_id]


def test_has_documents_given_a_document_in_the_collection_when_checked_then_returns_true(world: Any) -> None:
    target = upserted(world)
    put(world, target, document(1))

    assert world.documents.has_documents(target) is True


def test_has_documents_given_documents_only_in_another_collection_when_checked_then_returns_false(
    world: Any,
) -> None:
    put(world, upserted(world, "col-0000000000000001"), document(1))

    assert world.documents.has_documents(world.collection()) is False
