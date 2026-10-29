"""Full-snapshot document reconciliation shared by every connector."""

from collections.abc import Iterable
from dataclasses import dataclass
from tempfile import TemporaryFile
from typing import BinaryIO

from ccf_api_models.models import (
    BatchDeleteCustomConnectorDocumentsRequest,
    BatchPutCustomConnectorDocumentsRequest,
    DocumentId,
    DocumentItem,
    DocumentSummary,
    ListCustomConnectorDocumentsRequest,
)
from pydantic import BaseModel, ConfigDict, Field

from custom_connector_framework.client import CcfClient
from custom_connector_framework.models.document import Document, DocumentMetadata
from custom_connector_framework.sink import (
    DocumentDeleteRequest,
    DocumentSink,
    DocumentWriteResult,
    _DocumentWriteRequest,
)

_BATCH_MAX_SIZE = 10


@dataclass(frozen=True)
class _SpooledDocument:
    document_id: str
    filename: str
    metadata: DocumentMetadata
    checksum: str
    offset: int
    body_length: int


class SyncResult(BaseModel):
    """Internal outcome of one full-snapshot sync run."""

    model_config = ConfigDict(frozen=True)

    documents_written: int
    documents_deleted: int
    documents_unchanged: int


class ConnectorBinding(BaseModel):
    """Bind a connector identifier to the client used for tracked state."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid", frozen=True)

    connector_id: str = Field(..., max_length=128, min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
    client: CcfClient


class DocumentSynchronizer:
    """Reconcile one source snapshot with tracked state."""

    def __init__(self, sink: DocumentSink, binding: ConnectorBinding) -> None:
        self._sink = sink
        self._binding = binding

    def sync(self, documents: Iterable[Document]) -> SyncResult:
        """Preflight a snapshot, then write changes and remove absent documents."""
        with TemporaryFile(mode="w+b") as spool:
            spooled_documents = self._preflight_snapshot(documents, spool)
            del documents
            tracked_documents = self._list_documents()
            documents_to_write, unchanged_count = self._filter_changed(spooled_documents, tracked_documents)
            stale_document_ids = self._find_stale_document_ids(spooled_documents, tracked_documents)

            written_items = self._write_documents(documents_to_write, tracked_documents, spool)
            self._delete_stale_documents(stale_document_ids, tracked_documents)
            self._record_written_documents(written_items)
            self._untrack_deleted_documents(stale_document_ids)

            return SyncResult(
                documents_written=len(documents_to_write),
                documents_deleted=len(stale_document_ids),
                documents_unchanged=unchanged_count,
            )

    def _preflight_snapshot(self, documents: Iterable[Document], spool: BinaryIO) -> dict[str, _SpooledDocument]:
        """Validate and spool every body before state reads or mutations."""
        spooled_documents: dict[str, _SpooledDocument] = {}
        for document in documents:
            DocumentId(root=document.id)
            if document.id in spooled_documents:
                raise ValueError("Duplicate document IDs are not allowed in a snapshot.")
            offset = spool.tell()
            try:
                written_length = spool.write(document.content)
            except OSError:
                raise RuntimeError("Failed to spool the connector snapshot.") from None
            if written_length != len(document.content):
                raise RuntimeError("Failed to spool the connector snapshot.")
            spooled_documents[document.id] = _SpooledDocument(
                document_id=document.id,
                filename=document.filename,
                metadata=document.metadata,
                checksum=self._sink._reconciliation_checksum(document.get_checksum()),
                offset=offset,
                body_length=written_length,
            )
        try:
            spool.flush()
        except OSError:
            raise RuntimeError("Failed to spool the connector snapshot.") from None
        return spooled_documents

    def _list_documents(self) -> dict[str, DocumentSummary]:
        documents: dict[str, DocumentSummary] = {}
        next_token: str | None = None
        while True:
            response = self._binding.client.list_custom_connector_documents(
                ListCustomConnectorDocumentsRequest(
                    connector_id=self._binding.connector_id,
                    max_results=None,
                    next_token=next_token,
                )
            )
            for document in response.documents:
                if document.document_id in documents:
                    raise ValueError("CCF returned a duplicate tracked document ID.")
                documents[document.document_id] = document
            next_token = response.next_token
            if next_token is None:
                return documents

    @staticmethod
    def _filter_changed(
        documents: dict[str, _SpooledDocument],
        tracked_documents: dict[str, DocumentSummary],
    ) -> tuple[list[_SpooledDocument], int]:
        changed: list[_SpooledDocument] = []
        unchanged_count = 0
        for document_id, spooled_document in documents.items():
            tracked = tracked_documents.get(document_id)
            if tracked is None or tracked.checksum != spooled_document.checksum:
                changed.append(spooled_document)
            else:
                unchanged_count += 1
        return changed, unchanged_count

    @staticmethod
    def _find_stale_document_ids(
        documents: dict[str, _SpooledDocument],
        tracked_documents: dict[str, DocumentSummary],
    ) -> list[DocumentId]:
        return [DocumentId(root=document_id) for document_id in sorted(tracked_documents.keys() - documents.keys())]

    def _write_documents(
        self,
        documents: list[_SpooledDocument],
        tracked_documents: dict[str, DocumentSummary],
        spool: BinaryIO,
    ) -> list[DocumentItem]:
        written_items: list[DocumentItem] = []
        for spooled_document in documents:
            tracked = tracked_documents.get(spooled_document.document_id)
            document = self._rehydrate_document(spooled_document, spool)
            protected_object_keys = (tracked.object_key,) if tracked is not None else ()
            write_result = self._sink.put(
                _DocumentWriteRequest(
                    document=document,
                    protected_object_keys=protected_object_keys,
                )
            )
            del document
            self._delete_replaced_keys(tracked, write_result)
            written_items.append(
                DocumentItem(
                    document_id=spooled_document.document_id,
                    checksum=spooled_document.checksum,
                    object_key=write_result.object_key,
                    sidecar_key=write_result.sidecar_key,
                )
            )
        return written_items

    @staticmethod
    def _rehydrate_document(spooled_document: _SpooledDocument, spool: BinaryIO) -> Document:
        try:
            spool.seek(spooled_document.offset)
            content = spool.read(spooled_document.body_length)
        except OSError:
            raise RuntimeError("Failed to read the connector snapshot spool.") from None
        if len(content) != spooled_document.body_length:
            raise RuntimeError("Failed to read the connector snapshot spool.")
        return Document.from_bytes(
            document_id=spooled_document.document_id,
            content=content,
            filename=spooled_document.filename,
            metadata=spooled_document.metadata,
        )

    def _delete_replaced_keys(self, tracked: DocumentSummary | None, written: DocumentWriteResult) -> None:
        if tracked is None:
            return
        preserve_keys = tuple(key for key in (written.object_key, written.sidecar_key) if key is not None)
        self._sink.delete(
            DocumentDeleteRequest(
                object_key=tracked.object_key,
                sidecar_key=tracked.sidecar_key,
                preserve_keys=preserve_keys,
            )
        )

    def _delete_stale_documents(
        self,
        document_ids: list[DocumentId],
        tracked_documents: dict[str, DocumentSummary],
    ) -> None:
        for modeled_id in document_ids:
            tracked = tracked_documents[modeled_id.root]
            self._sink.delete(
                DocumentDeleteRequest(
                    object_key=tracked.object_key,
                    sidecar_key=tracked.sidecar_key,
                )
            )

    def _record_written_documents(self, documents: list[DocumentItem]) -> None:
        for start in range(0, len(documents), _BATCH_MAX_SIZE):
            self._binding.client.batch_put_custom_connector_documents(
                BatchPutCustomConnectorDocumentsRequest(
                    connector_id=self._binding.connector_id,
                    documents=documents[start : start + _BATCH_MAX_SIZE],
                )
            )

    def _untrack_deleted_documents(self, document_ids: list[DocumentId]) -> None:
        for start in range(0, len(document_ids), _BATCH_MAX_SIZE):
            self._binding.client.batch_delete_custom_connector_documents(
                BatchDeleteCustomConnectorDocumentsRequest(
                    connector_id=self._binding.connector_id,
                    document_ids=document_ids[start : start + _BATCH_MAX_SIZE],
                )
            )
