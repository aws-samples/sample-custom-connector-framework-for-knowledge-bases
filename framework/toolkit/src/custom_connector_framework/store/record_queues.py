"""Queues of document records waiting to be sent, one per collection.

A collection's queue is sent when it holds as many records as one `BatchPut` takes, or the
fullest queue when 1,000 records wait in all, at `save_checkpoint` for that collection, and at
flush. A queue sends its puts first, then its deletes. A document queued twice keeps only its
last action. Every send is reported to the store's counters.
"""

from __future__ import annotations

from ccf_api_models.base import ValueModel
from ccf_api_models.identifiers import CollectionIdentity
from ccf_api_models.models import (
    BatchDeleteCustomConnectorDocumentsRequest,
    BatchPutCustomConnectorDocumentsRequest,
    DocumentRecord,
)
from ccf_api_models.operations import CcfApi
from pydantic import BaseModel, ConfigDict, Field

from custom_connector_framework.store.identifiers import StoreTarget
from custom_connector_framework.store.report import SentRecords, StoreCounters

TOTAL_THRESHOLD = 1000
"""The store's own limit on records waiting in all collections."""

PUT_BATCH_SIZE: int = BatchPutCustomConnectorDocumentsRequest.model_json_schema()["properties"]["documents"]["maxItems"]
DELETE_BATCH_SIZE: int = BatchDeleteCustomConnectorDocumentsRequest.model_json_schema()["properties"]["document_ids"][
    "maxItems"
]


class QueuedPut(ValueModel):
    """A written or unchanged document's record, in its source collection."""

    source_collection_id: str | None
    record: DocumentRecord


class QueuedDelete(ValueModel):
    """A deleted document's ID, in its source collection."""

    source_collection_id: str | None
    document_id: str


class _CollectionQueue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    puts: dict[str, DocumentRecord] = Field(default_factory=dict)
    deletes: dict[str, None] = Field(default_factory=dict)

    def size(self) -> int:
        return len(self.puts) + len(self.deletes)


class _Batch(ValueModel):
    collection: CollectionIdentity
    queue: _CollectionQueue


class RecordQueues:
    """Holds and sends the records of every collection, and reports each send to the counters."""

    def __init__(self, api: CcfApi, target: StoreTarget, counters: StoreCounters) -> None:
        self._api = api
        self._target = target
        self._counters = counters
        self._queues: dict[str | None, _CollectionQueue] = {}

    def put(self, queued: QueuedPut) -> None:
        queue = self._queue(queued.source_collection_id)
        queue.deletes.pop(queued.record.document_id, None)
        queue.puts[queued.record.document_id] = queued.record
        self._send_over_threshold(queued.source_collection_id)

    def delete(self, queued: QueuedDelete) -> None:
        queue = self._queue(queued.source_collection_id)
        queue.puts.pop(queued.document_id, None)
        queue.deletes[queued.document_id] = None
        self._send_over_threshold(queued.source_collection_id)

    def count_pending(self, source_collection_id: str | None) -> int:
        queue = self._queues.get(source_collection_id)
        return 0 if queue is None else queue.size()

    def holds(self, source_collection_id: str | None) -> bool:
        """Whether any record of the collection is queued."""
        return self.count_pending(source_collection_id) > 0

    def queued_puts(self, source_collection_id: str | None) -> tuple[str, ...]:
        """The document IDs of the collection's queued puts, whose files are already in the bucket."""
        queue = self._queues.get(source_collection_id)
        return () if queue is None else tuple(queue.puts)

    def send(self, source_collection_id: str | None) -> None:
        """Send one collection's puts, then its deletes."""
        batch = self._batch(source_collection_id)
        self._send_puts(batch)
        self._send_deletes(batch)
        self._report(batch)

    def send_all(self) -> None:
        """Send every collection's queue."""
        for source_collection_id in list(self._queues):
            self.send(source_collection_id)

    def send_puts_then_deletes(self) -> None:
        """Send every collection's puts first and every collection's deletes after them, after a failed run."""
        batches = [self._batch(source_collection_id) for source_collection_id in list(self._queues)]
        for batch in batches:
            self._send_puts(batch)
        for batch in batches:
            self._send_deletes(batch)
        for batch in batches:
            self._report(batch)

    def _queue(self, source_collection_id: str | None) -> _CollectionQueue:
        self._counters.queued(source_collection_id)
        return self._queues.setdefault(source_collection_id, _CollectionQueue())

    def _batch(self, source_collection_id: str | None) -> _Batch:
        return _Batch(
            collection=CollectionIdentity.of(source_collection_id),
            queue=self._queues.pop(source_collection_id, _CollectionQueue()),
        )

    def _send_over_threshold(self, source_collection_id: str | None) -> None:
        if self.count_pending(source_collection_id) >= PUT_BATCH_SIZE:
            self.send(source_collection_id)
        if sum(queue.size() for queue in self._queues.values()) >= TOTAL_THRESHOLD:
            self.send(max(self._queues, key=lambda collection: self._queues[collection].size()))

    def _send_puts(self, batch: _Batch) -> None:
        collection = batch.collection
        records = list(batch.queue.puts.values())
        for start in range(0, len(records), PUT_BATCH_SIZE):
            self._api.call(
                BatchPutCustomConnectorDocumentsRequest(
                    destination_id=self._target.destination_id,
                    connector_id=self._target.connector_id,
                    documents=records[start : start + PUT_BATCH_SIZE],
                    # The default collection's source ID is left out: the contract doesn't let it be null.
                    **collection.model_dump(exclude_none=True),
                )
            )

    def _send_deletes(self, batch: _Batch) -> None:
        document_ids = list(batch.queue.deletes)
        for start in range(0, len(document_ids), DELETE_BATCH_SIZE):
            self._api.call(
                BatchDeleteCustomConnectorDocumentsRequest(
                    destination_id=self._target.destination_id,
                    connector_id=self._target.connector_id,
                    collection_id=batch.collection.collection_id,
                    document_ids=document_ids[start : start + DELETE_BATCH_SIZE],
                )
            )

    def _report(self, batch: _Batch) -> None:
        self._counters.sent(
            SentRecords(
                source_collection_id=batch.collection.source_collection_id,
                puts=len(batch.queue.puts),
                deletes=len(batch.queue.deletes),
            )
        )
