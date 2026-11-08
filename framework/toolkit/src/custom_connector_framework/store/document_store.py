"""The `DocumentStore`: how a connector implementation hands documents, deletes, and checkpoints to the framework.

`put` writes the document's metadata file and file to the destination bucket through the
source-access session, unless the stored file already has the same content hash, and queues
the document's record. `delete` queues a delete. Queued records go to the framework API in
batches (`record_queues.py`), and a collection's checkpoint is saved after its queue is sent,
so a saved checkpoint never covers a record the framework doesn't hold.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

from annotated_types import MaxLen
from ccf_api_models.base import ValueModel
from ccf_api_models.identifiers import CollectionIdentity, SourceIdKind
from ccf_api_models.models import (
    BatchDeleteCustomConnectorCollectionsRequest,
    CollectionReference,
    DocumentRecord,
    GetCustomConnectorCheckpointRequest,
    ListCustomConnectorCollectionsRequest,
    ListCustomConnectorCollectionsResponseContent,
    ListCustomConnectorDocumentsRequest,
    ListCustomConnectorDocumentsResponseContent,
    PutCustomConnectorCheckpointRequest,
)
from ccf_api_models.operations import CcfApi
from pydantic import BaseModel, SecretStr

from custom_connector_framework.client.pages import Pages, PagesRequest
from custom_connector_framework.store.content_reader import (
    ContentReader,
    ReadContentRequest,
    SpooledContent,
    UnreadableTextError,
)
from custom_connector_framework.store.destination_formats import (
    AccessControlUse,
    DestinationFormat,
    MetadataFileRequest,
)
from custom_connector_framework.store.file_writer import FileWriter, WriteFileRequest, WriteOutcome
from custom_connector_framework.store.identifiers import CollectionDocument, StoreTarget
from custom_connector_framework.store.media_types import DETECTION_BYTES, MediaTypeRequest, MediaTypes, StoredMedia
from custom_connector_framework.store.models import (
    DeleteCollectionRequest,
    DeleteDocumentRequest,
    DiffRequest,
    DiffResponse,
    GetCheckpointRequest,
    ListDocumentsRequest,
    PutDocumentRequest,
    SaveCheckpointRequest,
    SkipReason,
    StoredCollection,
    StoredDocument,
    StoreReport,
)
from custom_connector_framework.store.record_queues import QueuedDelete, QueuedPut, RecordQueues
from custom_connector_framework.store.report import SkippedDocument, StoreCounters

logger = logging.getLogger(__name__)

_CHECKPOINT_BYTE_LIMIT = next(
    item.max_length
    for item in PutCustomConnectorCheckpointRequest.model_fields["checkpoint_data"].metadata
    if isinstance(item, MaxLen)
)
"""The generated `CheckpointData` maximum, which the framework also applies to the text's UTF-8 bytes."""


class _Skip(ValueModel):
    request: PutDocumentRequest
    reason: SkipReason


class _Write(ValueModel):
    request: PutDocumentRequest
    media: StoredMedia
    content: SpooledContent
    access_control_use: AccessControlUse


class DocumentStore:
    """Stores documents and checkpoints for one custom connector's job. A resource manager: several public methods."""

    def __init__(
        self,
        target: StoreTarget,
        api: CcfApi,
        pages: Pages,
        destination_format: DestinationFormat,
        media_types: MediaTypes,
        reader: ContentReader,
        files: FileWriter,
        queues: RecordQueues,
        counters: StoreCounters,
    ) -> None:
        self._target = target
        self._api = api
        self._pages = pages
        self._format = destination_format
        self._media_types = media_types
        self._reader = reader
        self._files = files
        self._queues = queues
        self._counters = counters
        self._stored_document_ids: dict[str | None, frozenset[str]] = {}
        """Each collection's stored document IDs, read once, when a put in it is first skipped."""
        self._put_document_ids: set[tuple[str | None, str]] = set()

    def put(self, request: PutDocumentRequest) -> None:
        """Write the document unless its stored file is unchanged, and queue its record.

        A skipped document the framework holds, or that this job put earlier, gets a queued delete,
        so an earlier stored version doesn't stay searchable.
        """
        access_control_use = self._format.check_access_control(request.access_control)
        head = request.content.read(DETECTION_BYTES)
        media = self._media_types.resolve(
            MediaTypeRequest(
                file_name=request.file_name,
                head=head,
                media_type=request.media_type,
                supported=self._format.supported_types,
            )
        )
        if media is None:
            self._skip(_Skip(request=request, reason=SkipReason.UNSUPPORTED_CONTENT))
            return
        try:
            content = self._reader.read(ReadContentRequest(head=head, rest=request.content, is_text=media.is_text))
        except UnreadableTextError:
            self._skip(_Skip(request=request, reason=SkipReason.UNSUPPORTED_CONTENT))
            return
        with content:
            if media.is_text and content.blank:
                self._skip(_Skip(request=request, reason=SkipReason.EMPTY_CONTENT))
                return
            self._write(_Write(request=request, media=media, content=content, access_control_use=access_control_use))

    def delete(self, request: DeleteDocumentRequest) -> None:
        """Queue a delete; the framework ignores a document it doesn't hold."""
        document_id = SourceIdKind.DOCUMENT.of(request.document_id)
        logger.info("document_delete_queued", extra={"document_id": document_id, "reason": "deleted_at_source"})
        self._queues.delete(QueuedDelete(source_collection_id=request.collection_id, document_id=document_id))

    def documents(self, request: ListDocumentsRequest) -> Iterator[StoredDocument]:
        """Yield the collection's stored documents by source ID; changes nothing."""
        listing = ListCustomConnectorDocumentsRequest(
            destination_id=self._target.destination_id,
            connector_id=self._target.connector_id,
            collection_id=CollectionIdentity.of(request.collection_id).collection_id,
        )
        pages = self._pages.of(
            PagesRequest[ListCustomConnectorDocumentsResponseContent](
                request=listing, response_type=ListCustomConnectorDocumentsResponseContent
            )
        )
        for page in pages:
            for stored in page.documents:
                yield StoredDocument(
                    document_id=stored.source_document_id,
                    source_version=stored.source_version,
                    updated_at=stored.updated_at,
                )

    def diff(self, request: DiffRequest) -> DiffResponse:
        """Compare the source's current documents with the stored ones; changes nothing."""
        stored = {
            document.document_id: document.source_version
            for document in self.documents(ListDocumentsRequest(collection_id=request.collection_id))
        }
        current = {document.document_id: document.source_version for document in request.current}
        return DiffResponse(
            new=tuple(document_id for document_id in current if document_id not in stored),
            changed=tuple(
                document_id
                for document_id, version in current.items()
                if document_id in stored and stored[document_id] != version
            ),
            missing=tuple(document_id for document_id in stored if document_id not in current),
        )

    def collections(self) -> Iterator[StoredCollection]:
        """Yield each stored collection's source ID (None for the default collection) and checkpoint.

        Each listed collection counts as read, so one the job then leaves alone is reported unchanged.
        """
        listing = ListCustomConnectorCollectionsRequest(
            destination_id=self._target.destination_id, connector_id=self._target.connector_id
        )
        for collection in self._listed_collections(listing):
            self._counters.checkpoint_read(collection.collection_id)
            yield collection

    def get_checkpoint[CheckpointT: BaseModel](self, request: GetCheckpointRequest[CheckpointT]) -> CheckpointT | None:
        """The collection's checkpoint as the request's model; None when none is stored or the text doesn't parse."""
        self._counters.checkpoint_read(request.collection_id)
        response = self._api.call(
            GetCustomConnectorCheckpointRequest(
                destination_id=self._target.destination_id,
                connector_id=self._target.connector_id,
                collection_id=CollectionIdentity.of(request.collection_id).collection_id,
            )
        )
        checkpoint = response.checkpoint
        stored = StoredCollection(
            collection_id=request.collection_id,
            checkpoint=None if checkpoint is None else checkpoint.checkpoint_data.get_secret_value(),
        )
        return stored.parse_checkpoint(request.checkpoint_type)

    def save_checkpoint(self, request: SaveCheckpointRequest) -> None:
        """Send the collection's queued records, then save its checkpoint; one over the limits fails before any send."""
        collection = CollectionIdentity.of(request.collection_id)
        checkpoint = PutCustomConnectorCheckpointRequest(
            destination_id=self._target.destination_id,
            connector_id=self._target.connector_id,
            checkpoint_data=SecretStr(request.checkpoint),
            # The default collection's source ID is left out: the contract doesn't let it be null.
            **collection.model_dump(exclude_none=True),
        )
        if len(request.checkpoint.encode("utf-8")) > _CHECKPOINT_BYTE_LIMIT:
            raise ValueError(f"A checkpoint must be at most {_CHECKPOINT_BYTE_LIMIT} UTF-8 bytes")
        self._queues.send(request.collection_id)
        self._api.call(checkpoint)
        self._counters.checkpoint_saved(request.collection_id)

    def delete_collection(self, request: DeleteCollectionRequest) -> None:
        """Delete the collection's documents in batches of 99 and then the collection.

        A collection that is only queued counts too, and every queued put becomes a delete, because
        its file is already in the bucket. A collection neither stored nor queued is ignored.
        """
        collection = CollectionIdentity.of(request.collection_id)
        lookup = ListCustomConnectorCollectionsRequest(
            destination_id=self._target.destination_id,
            connector_id=self._target.connector_id,
            collection_id=collection.collection_id,
        )
        stored = any(True for _ in self._listed_collections(lookup))
        if not stored and not self._queues.holds(request.collection_id):
            return
        document_ids = list(self._queues.queued_puts(request.collection_id))
        if stored:
            document_ids += [
                SourceIdKind.DOCUMENT.of(document.document_id)
                for document in self.documents(ListDocumentsRequest(collection_id=request.collection_id))
            ]
        for document_id in dict.fromkeys(document_ids):
            logger.info("document_delete_queued", extra={"document_id": document_id, "reason": "collection_deleted"})
            self._queues.delete(QueuedDelete(source_collection_id=request.collection_id, document_id=document_id))
        self._queues.send(request.collection_id)
        self._api.call(
            BatchDeleteCustomConnectorCollectionsRequest(
                destination_id=self._target.destination_id,
                connector_id=self._target.connector_id,
                collections=[CollectionReference(collection_id=collection.collection_id)],
            )
        )
        self._counters.collection_deleted(request.collection_id)
        logger.info("collection_deleted", extra={"collection_id": collection.collection_id})

    def flush(self) -> None:
        """Send every collection's queued records: each collection's puts, then its deletes."""
        self._queues.send_all()
        self._warn_about_access_control()

    def flush_after_failure(self) -> None:
        """Send every queued put, then every queued delete, after the implementation raised."""
        self._queues.send_puts_then_deletes()
        self._warn_about_access_control()

    def get_report(self) -> StoreReport:
        """Return per-collection and total counts; changes nothing."""
        return self._counters.report(self._queues)

    def _write(self, write: _Write) -> None:
        request = write.request
        collection = CollectionIdentity.of(request.collection_id)
        document_id = SourceIdKind.DOCUMENT.of(request.document_id)
        document = self._target.document(
            CollectionDocument(collection_id=collection.collection_id, document_id=document_id)
        )
        metadata_json = self._format.metadata_file(
            MetadataFileRequest(
                request=request, document_id=document_id, document_arn=document.arn, stored_name=write.media.stored_name
            )
        )
        content_hash = write.content.content_hash(metadata_json)
        # The record checks the API's limits on the source ID and version before any file is written.
        record = DocumentRecord(
            document_id=document_id,
            source_document_id=request.document_id,
            content_hash=content_hash,
            source_version=request.source_version,
        )
        outcome = self._files.write(
            WriteFileRequest(
                prefix=document.key.prefix,
                stored_name=write.media.stored_name,
                metadata_json=metadata_json,
                content=write.content,
                content_hash=content_hash,
            )
        )
        if write.access_control_use is AccessControlUse.LEFT_OUT:
            self._counters.access_control_left_out()
        self._put_document_ids.add((request.collection_id, document_id))
        if outcome is WriteOutcome.WRITTEN:
            self._counters.written(request.collection_id)
        else:
            self._counters.unchanged(request.collection_id)
        self._queues.put(QueuedPut(source_collection_id=request.collection_id, record=record))

    def _listed_collections(self, listing: ListCustomConnectorCollectionsRequest) -> Iterator[StoredCollection]:
        pages = self._pages.of(
            PagesRequest[ListCustomConnectorCollectionsResponseContent](
                request=listing, response_type=ListCustomConnectorCollectionsResponseContent
            )
        )
        for page in pages:
            for collection in page.collections:
                checkpoint = collection.checkpoint_data
                yield StoredCollection(
                    collection_id=collection.source_collection_id,
                    checkpoint=None if checkpoint is None else checkpoint.get_secret_value(),
                )

    def _skip(self, skip: _Skip) -> None:
        """Count and log the skip; delete a version the framework holds, so it doesn't stay searchable."""
        request = skip.request
        document_id = SourceIdKind.DOCUMENT.of(request.document_id)
        self._counters.skipped(SkippedDocument(source_collection_id=request.collection_id, reason=skip.reason))
        logger.info("document_skipped", extra={"document_id": document_id, "reason": skip.reason.value})
        held = (request.collection_id, document_id) in self._put_document_ids or document_id in self._stored_in(
            request.collection_id
        )
        if held:
            self._queues.delete(QueuedDelete(source_collection_id=request.collection_id, document_id=document_id))

    def _stored_in(self, collection_id: str | None) -> frozenset[str]:
        """The collection's stored document IDs, listed once per job."""
        if collection_id not in self._stored_document_ids:
            self._stored_document_ids[collection_id] = frozenset(
                SourceIdKind.DOCUMENT.of(document.document_id)
                for document in self.documents(ListDocumentsRequest(collection_id=collection_id))
            )
        return self._stored_document_ids[collection_id]

    def _warn_about_access_control(self) -> None:
        left_out = self._counters.take_access_control_left_out()
        if left_out:
            logger.warning(
                "access_control_lists_not_written",
                extra={
                    "document_count": left_out,
                    "reason": "the BEDROCK destination has access control lists turned off",
                },
            )
