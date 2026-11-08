"""DynamoDB records of documents (`CcfDocuments`).

Documents are keyed by custom connector ARN and `<collection_id>#<document_id>`, so one query
lists a collection's documents. Every batch is one transaction with its collection's write guard,
so a batch never lands in a collection being deleted.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from ccf_api_models.base import ValueModel
from ccf_api_models.contracts import TablesContract
from ccf_api_models.identifiers import CollectionRef, ConnectorRef, DocumentRef
from ccf_api_models.models import DocumentRecord, StoredDocument
from pydantic import ConfigDict

from common.resource_ids import Clock
from common.storage.ddb.model_table import (
    AttributeValue,
    DeleteItemRequest,
    ItemKey,
    ListItemsRequest,
    ModelTable,
    Page,
    QueryRequest,
    ReadConsistency,
    SortCondition,
    SortOperator,
    TransactionRequest,
    TransactWrite,
    UpdateItemRequest,
)


class _DocumentItem(StoredDocument):
    model_config = ConfigDict(extra="ignore", frozen=True)


class PutDocumentsRecord(ValueModel):
    """Documents to record in one collection, all or none, and the collection's write guard."""

    collection: CollectionRef
    documents: tuple[DocumentRecord, ...]
    guard: TransactWrite


class DeleteDocumentsRecord(ValueModel):
    """Document records to delete from one collection, all or none, and the collection's write guard."""

    collection: CollectionRef
    document_ids: tuple[str, ...]
    guard: TransactWrite


class DocumentsTable(ValueModel):
    """The boto3 `CcfDocuments` table, the DynamoDB client its transactions use, and the clock."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    table: Any
    client: Any
    clock: Clock


class DocumentsDao:
    """Records and deletes documents in transactions guarded by their collection's item."""

    def __init__(self, table: ModelTable[_DocumentItem], clock: Clock) -> None:
        self._table = table
        self._clock = clock

    @classmethod
    def over(cls, request: DocumentsTable) -> DocumentsDao:
        contract = TablesContract.load().documents
        return cls(ModelTable(request.table, request.client, _DocumentItem, contract), request.clock)

    def has_documents(self, collection: CollectionRef) -> bool:
        """Whether the collection holds a document, from a strongly consistent read of one item."""
        return bool(self._table.query(_collection_query(collection)).items)

    def put_documents(self, record: PutDocumentsRecord) -> None:
        """Each record is an update, so a rewritten document keeps its first `created_at`."""
        now = self._clock.now()
        writes = [
            self._update_action(_DocumentWrite(collection=record.collection, document=d, now=now))
            for d in record.documents
        ]
        self._table.transact(TransactionRequest(action="Document recording", writes=(*writes, record.guard)))

    def delete_documents(self, record: DeleteDocumentsRecord) -> None:
        """A missing record passes; the guard alone decides whether the batch may run."""
        writes = [
            self._table.delete_action(
                DeleteItemRequest(
                    key=_key(DocumentRef(collection=record.collection, document_id=document_id)),
                    action="Document deletion",
                )
            )
            for document_id in record.document_ids
        ]
        self._table.transact(TransactionRequest(action="Document deletion", writes=(*writes, record.guard)))

    def list(self, request: ListItemsRequest[ConnectorRef]) -> Page[StoredDocument]:
        """One page in collection and document ID order; the filter value narrows it to one collection."""
        collection_id = request.filter_value
        page = self._table.query(
            QueryRequest(
                partition_value=request.parent.arn,
                sort_condition=None
                if collection_id is None
                else SortCondition(operator=SortOperator.BEGINS_WITH, value=f"{collection_id}#"),
                max_results=request.max_results,
                next_token=request.next_token,
            )
        )
        return Page(
            items=[StoredDocument.model_validate(item.model_dump()) for item in page.items], next_token=page.next_token
        )

    def _update_action(self, write: _DocumentWrite) -> TransactWrite:
        document = write.document
        values: dict[str, AttributeValue] = {
            ":collection_id": write.collection.collection_id,
            ":document_id": document.document_id,
            ":source_document_id": document.source_document_id,
            ":content_hash": document.content_hash,
            ":now": write.now,
        }
        sets = [f"{name} = :{name}" for name in ("collection_id", "document_id", "source_document_id", "content_hash")]
        sets += ["created_at = if_not_exists(created_at, :now)", "updated_at = :now"]
        if document.source_version is not None:
            sets.append("source_version = :source_version")
            values[":source_version"] = document.source_version
        remove = "" if document.source_version is not None else " REMOVE source_version"
        return self._table.update_action(
            UpdateItemRequest(
                key=_key(DocumentRef(collection=write.collection, document_id=document.document_id)),
                action="Document recording",
                expression=f"SET {', '.join(sets)}{remove}",
                values=values,
            )
        )


class _DocumentWrite(ValueModel):
    collection: CollectionRef
    document: DocumentRecord
    now: Decimal


def _key(document: DocumentRef) -> ItemKey:
    collection = document.collection
    return ItemKey(partition=collection.connector.arn, sort=f"{collection.collection_id}#{document.document_id}")


def _collection_query(collection: CollectionRef) -> QueryRequest:
    return QueryRequest(
        partition_value=collection.connector.arn,
        sort_condition=SortCondition(operator=SortOperator.BEGINS_WITH, value=f"{collection.collection_id}#"),
        max_results=1,
        consistency=ReadConsistency.STRONG,
    )
