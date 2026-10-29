"""Tests for shared full-snapshot reconciliation."""

import hashlib
import tempfile
import weakref
from collections.abc import Iterable, Iterator
from pathlib import Path

import pytest
from ccf_api_models.models import (
    BatchDeleteCustomConnectorDocumentsRequest,
    BatchPutCustomConnectorDocumentsRequest,
    DocumentSummary,
    ListCustomConnectorDocumentsRequest,
    ListCustomConnectorDocumentsResponseContent,
)

from custom_connector_framework import interface as interface_module
from custom_connector_framework.client import CcfClient
from custom_connector_framework.interface import ConnectorBinding, DocumentSynchronizer
from custom_connector_framework.models.document import Document
from custom_connector_framework.models.exceptions import SinkDeleteError
from custom_connector_framework.object_store import DeleteObjectRequest, LocalFileObjectStore
from custom_connector_framework.providers.quick import QuickKnowledgeBaseAdapter
from custom_connector_framework.sink import (
    DocumentDeleteRequest,
    DocumentSink,
    DocumentWriteResult,
    SinkConfig,
    _DocumentWriteRequest,
)
from tests.conftest import make_document


class FakeSink:
    def __init__(self, keys: dict[str, tuple[str, str | None]] | None = None) -> None:
        self._keys = keys or {}
        self.written: list[str] = []
        self.deleted: list[DocumentDeleteRequest] = []

    def _reconciliation_checksum(self, document_checksum: str) -> str:
        return document_checksum

    def put(self, request: _DocumentWriteRequest) -> DocumentWriteResult:
        document = request.document
        self.written.append(document.id)
        object_key, sidecar_key = self._keys.get(
            document.id,
            (f"objects/{document.id}.txt", f"objects/{document.id}.txt.metadata.json"),
        )
        return DocumentWriteResult(object_key=object_key, sidecar_key=sidecar_key)

    def delete(self, request: DocumentDeleteRequest) -> None:
        self.deleted.append(request)


class FakeCcfClient(CcfClient):
    def __init__(self) -> None:
        self.document_pages: list[ListCustomConnectorDocumentsResponseContent] = []
        self.list_requests: list[ListCustomConnectorDocumentsRequest] = []
        self.put_batches: list[BatchPutCustomConnectorDocumentsRequest] = []
        self.delete_batches: list[BatchDeleteCustomConnectorDocumentsRequest] = []

    def list_custom_connector_documents(
        self,
        request: ListCustomConnectorDocumentsRequest,
    ) -> ListCustomConnectorDocumentsResponseContent:
        self.list_requests.append(request)
        return self.document_pages.pop(0)

    def batch_put_custom_connector_documents(self, request: BatchPutCustomConnectorDocumentsRequest) -> None:
        self.put_batches.append(request)

    def batch_delete_custom_connector_documents(self, request: BatchDeleteCustomConnectorDocumentsRequest) -> None:
        self.delete_batches.append(request)


class FaultInjectingCcfClient(CcfClient):
    def __init__(self) -> None:
        self.state: dict[str, DocumentSummary] = {}
        self.put_failures_remaining = 0
        self.delete_failures_remaining = 0

    def list_custom_connector_documents(
        self,
        request: ListCustomConnectorDocumentsRequest,
    ) -> ListCustomConnectorDocumentsResponseContent:
        assert request.connector_id == "connector-1"
        return _page(list(self.state.values()))

    def batch_put_custom_connector_documents(self, request: BatchPutCustomConnectorDocumentsRequest) -> None:
        if self.put_failures_remaining:
            self.put_failures_remaining -= 1
            raise RuntimeError("control-plane batch put failed")
        for document in request.documents:
            self.state[document.document_id] = DocumentSummary(
                document_id=document.document_id,
                checksum=document.checksum,
                object_key=document.object_key,
                sidecar_key=document.sidecar_key,
                created_at=1.0,
                updated_at=1.0,
            )

    def batch_delete_custom_connector_documents(self, request: BatchDeleteCustomConnectorDocumentsRequest) -> None:
        if self.delete_failures_remaining:
            self.delete_failures_remaining -= 1
            raise RuntimeError("control-plane batch delete failed")
        for document_id in request.document_ids:
            self.state.pop(document_id.root, None)


def _summary(
    document_id: str,
    checksum: str,
    object_key: str | None = None,
    sidecar_key: str | None = None,
) -> DocumentSummary:
    return DocumentSummary(
        document_id=document_id,
        checksum=checksum,
        object_key=object_key or f"old/{document_id}.txt",
        sidecar_key=sidecar_key,
        created_at=1.0,
        updated_at=2.0,
    )


def _page(
    documents: list[DocumentSummary],
    next_token: str | None = None,
) -> ListCustomConnectorDocumentsResponseContent:
    return ListCustomConnectorDocumentsResponseContent(documents=documents, next_token=next_token)


def _synchronizer(client: FakeCcfClient, sink: FakeSink) -> DocumentSynchronizer:
    return DocumentSynchronizer(
        sink=sink,  # type: ignore[arg-type]
        binding=ConnectorBinding(connector_id="connector-1", client=client),
    )


def _persistent_synchronizer(
    client: FaultInjectingCcfClient,
    object_store: LocalFileObjectStore,
) -> DocumentSynchronizer:
    return DocumentSynchronizer(
        sink=DocumentSink(
            object_store=object_store,
            config=SinkConfig(prefix="objects"),
            adapter=QuickKnowledgeBaseAdapter(),
        ),
        binding=ConnectorBinding(connector_id="connector-1", client=client),
    )


def _stored_files(root: Path) -> set[str]:
    return {str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()}


def test_sync_reconciles_changed_unchanged_and_stale_documents() -> None:
    unchanged = make_document("github-owner-repo-a-issue-1", b"same")
    changed = make_document("gitlab-group-repo-b-merge-request-2", b"new")
    stale_id = "github-owner-repo-c-issue-3"
    client = FakeCcfClient()
    client.document_pages.append(
        _page(
            [
                _summary(unchanged.id, unchanged.get_checksum()),
                _summary(changed.id, "old-checksum", "old/changed.java", "old/changed.java.metadata.json"),
                _summary(stale_id, "stale", "old/stale.md", "old/stale.md.metadata.json"),
            ]
        )
    )
    sink = FakeSink({changed.id: ("new/changed.java.txt", "new/changed.java.txt.metadata.json")})

    result = _synchronizer(client, sink).sync([unchanged, changed])

    assert result.documents_written == 1
    assert result.documents_unchanged == 1
    assert result.documents_deleted == 1
    assert sink.written == [changed.id]
    assert sink.deleted == [
        DocumentDeleteRequest(
            object_key="old/changed.java",
            sidecar_key="old/changed.java.metadata.json",
            preserve_keys=("new/changed.java.txt", "new/changed.java.txt.metadata.json"),
        ),
        DocumentDeleteRequest(
            object_key="old/stale.md",
            sidecar_key="old/stale.md.metadata.json",
        ),
    ]
    assert client.put_batches[0].documents[0].object_key == "new/changed.java.txt"
    assert client.delete_batches[0].document_ids[0].root == stale_id


def test_sync_consumes_all_state_pages() -> None:
    client = FakeCcfClient()
    client.document_pages.extend([_page([], "page-2"), _page([])])

    _synchronizer(client, FakeSink()).sync([])

    assert [request.next_token for request in client.list_requests] == [None, "page-2"]


def test_sync_batches_state_updates() -> None:
    documents = [make_document(f"doc-{index}", f"body-{index}".encode()) for index in range(23)]
    stale = [_summary(f"gone-{index}", f"checksum-{index}") for index in range(12)]
    client = FakeCcfClient()
    client.document_pages.append(_page(stale))

    _synchronizer(client, FakeSink()).sync(documents)

    assert [len(batch.documents) for batch in client.put_batches] == [10, 10, 3]
    assert [len(batch.document_ids) for batch in client.delete_batches] == [10, 2]


def test_sync_retries_after_batch_put_fails_after_destination_write(tmp_path: Path) -> None:
    root = tmp_path / "store"
    client = FaultInjectingCcfClient()
    client.put_failures_remaining = 1
    synchronizer = _persistent_synchronizer(client, LocalFileObjectStore(root))
    document = make_document("doc-1")

    with pytest.raises(RuntimeError, match="batch put failed"):
        synchronizer.sync([document])

    assert client.state == {}
    assert len(_stored_files(root)) == 2

    result = synchronizer.sync([document])

    tracked = client.state[document.id]
    assert result.documents_written == 1
    expected_checksum = hashlib.sha256(f"{document.get_checksum()}\0quick:1".encode()).hexdigest()
    assert tracked.checksum == expected_checksum
    assert _stored_files(root) == {tracked.object_key, tracked.sidecar_key}


def test_sync_retries_after_batch_delete_fails_after_destination_delete(tmp_path: Path) -> None:
    root = tmp_path / "store"
    client = FaultInjectingCcfClient()
    synchronizer = _persistent_synchronizer(client, LocalFileObjectStore(root))
    document = make_document("doc-1")
    synchronizer.sync([document])
    tracked = client.state[document.id]
    client.delete_failures_remaining = 1

    with pytest.raises(RuntimeError, match="batch delete failed"):
        synchronizer.sync([])

    assert client.state[document.id] == tracked
    assert _stored_files(root) == set()

    result = synchronizer.sync([])

    assert result.documents_deleted == 1
    assert client.state == {}
    assert _stored_files(root) == set()


def test_sync_preserves_tracked_revision_when_old_object_delete_fails_and_retry_converges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "store"
    client = FaultInjectingCcfClient()
    object_store = LocalFileObjectStore(root)
    synchronizer = _persistent_synchronizer(client, object_store)
    prior_document = make_document("doc-1", content=b"prior")
    synchronizer.sync([prior_document])
    prior = client.state[prior_document.id]
    updated_document = make_document("doc-1", content=b"updated")
    updated_object_key = f"objects/doc-1/{updated_document.get_checksum()}.txt"
    original_delete = object_store.delete_object
    failure_injected = False

    def fail_old_object_once(request: DeleteObjectRequest) -> None:
        nonlocal failure_injected
        if request.key == prior.object_key and not failure_injected:
            failure_injected = True
            raise RuntimeError("object delete failed")
        original_delete(request)

    monkeypatch.setattr(object_store, "delete_object", fail_old_object_once)

    with pytest.raises(SinkDeleteError):
        synchronizer.sync([updated_document])

    assert client.state[prior_document.id] == prior
    assert _stored_files(root) == {
        prior.object_key,
        prior.sidecar_key,
        updated_object_key,
        f"{updated_object_key}.metadata.json",
    }

    synchronizer.sync([updated_document])

    tracked = client.state[updated_document.id]
    assert tracked.object_key == updated_object_key
    assert _stored_files(root) == {tracked.object_key, tracked.sidecar_key}


def test_sync_keeps_state_when_sidecar_delete_fails_and_retry_converges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "store"
    client = FaultInjectingCcfClient()
    object_store = LocalFileObjectStore(root)
    synchronizer = _persistent_synchronizer(client, object_store)
    document = make_document("doc-1")
    synchronizer.sync([document])
    tracked = client.state[document.id]
    assert tracked.sidecar_key is not None
    original_delete = object_store.delete_object
    failure_injected = False

    def fail_sidecar_once(request: DeleteObjectRequest) -> None:
        nonlocal failure_injected
        if request.key == tracked.sidecar_key and not failure_injected:
            failure_injected = True
            raise RuntimeError("sidecar delete failed")
        original_delete(request)

    monkeypatch.setattr(object_store, "delete_object", fail_sidecar_once)

    with pytest.raises(SinkDeleteError):
        synchronizer.sync([])

    assert client.state[document.id] == tracked
    assert _stored_files(root) == {tracked.sidecar_key}

    synchronizer.sync([])

    assert client.state == {}
    assert _stored_files(root) == set()


def test_sync_rejects_duplicate_ids_before_state_read_or_mutation() -> None:
    document = make_document("doc-1")
    client = FakeCcfClient()
    sink = FakeSink()

    with pytest.raises(ValueError, match="Duplicate document IDs"):
        _synchronizer(client, sink).sync([document, document])

    assert client.list_requests == []
    assert sink.written == []
    assert sink.deleted == []


def test_sync_does_not_read_state_or_mutate_when_enumeration_fails() -> None:
    client = FakeCcfClient()
    sink = FakeSink()

    def failing_snapshot() -> Iterator[Document]:
        yield make_document("doc-1")
        raise RuntimeError("source enumeration failed")

    with pytest.raises(RuntimeError, match="source enumeration failed"):
        _synchronizer(client, sink).sync(failing_snapshot())

    assert client.list_requests == []
    assert client.put_batches == []
    assert client.delete_batches == []
    assert sink.written == []
    assert sink.deleted == []


def test_sync_does_not_read_state_or_mutate_when_selected_content_read_fails(tmp_path: Path) -> None:
    readable = tmp_path / "readable.txt"
    readable.write_bytes(b"first")
    missing = tmp_path / "missing.txt"
    client = FakeCcfClient()
    sink = FakeSink()

    def source_snapshot() -> Iterable[Document]:
        yield make_document("doc-1", readable.read_bytes())
        yield make_document("doc-2", missing.read_bytes())

    with pytest.raises(FileNotFoundError):
        _synchronizer(client, sink).sync(source_snapshot())

    assert client.list_requests == []
    assert client.put_batches == []
    assert client.delete_batches == []
    assert sink.written == []
    assert sink.deleted == []


class OneDocumentAtATimeSink(FakeSink):
    def __init__(self) -> None:
        super().__init__()
        self._previous_document: weakref.ReferenceType[Document] | None = None
        self.bodies: list[bytes] = []

    def put(self, request: _DocumentWriteRequest) -> DocumentWriteResult:
        document = request.document
        if self._previous_document is not None:
            assert self._previous_document() is None
        self._previous_document = weakref.ref(document)
        self.bodies.append(document.content)
        return super().put(request)


class FailingTemporaryFile:
    def __init__(self) -> None:
        self.closed = False

    def __enter__(self) -> FailingTemporaryFile:
        return self

    def __exit__(self, *args: object) -> None:
        self.closed = True

    def tell(self) -> int:
        return 0

    def write(self, body: bytes) -> int:
        raise OSError("private temporary path")


class FailingSink(FakeSink):
    def put(self, request: _DocumentWriteRequest) -> DocumentWriteResult:
        raise RuntimeError("destination failed")


def test_sync_releases_source_bodies_before_state_and_rehydrates_one_at_a_time() -> None:
    source_documents: list[weakref.ReferenceType[Document]] = []

    def documents() -> Iterator[Document]:
        for document_id, body in (("doc-1", b"first"), ("doc-2", b"second")):
            document = make_document(document_id, body)
            source_documents.append(weakref.ref(document))
            yield document

    class PreflightInspectingClient(FakeCcfClient):
        def list_custom_connector_documents(
            self,
            request: ListCustomConnectorDocumentsRequest,
        ) -> ListCustomConnectorDocumentsResponseContent:
            assert all(document() is None for document in source_documents)
            return super().list_custom_connector_documents(request)

    client = PreflightInspectingClient()
    client.document_pages.append(_page([]))
    sink = OneDocumentAtATimeSink()

    _synchronizer(client, sink).sync(documents())

    assert sink.bodies == [b"first", b"second"]


def test_sync_releases_materialized_source_bodies_before_state_and_rehydrates_one_at_a_time() -> None:
    source_documents: list[weakref.ReferenceType[Document]] = []

    def materialized_documents() -> list[Document]:
        documents = [make_document("doc-1", b"first"), make_document("doc-2", b"second")]
        source_documents.extend(weakref.ref(document) for document in documents)
        return documents

    class PreflightInspectingClient(FakeCcfClient):
        def list_custom_connector_documents(
            self,
            request: ListCustomConnectorDocumentsRequest,
        ) -> ListCustomConnectorDocumentsResponseContent:
            assert all(document() is None for document in source_documents)
            return super().list_custom_connector_documents(request)

    client = PreflightInspectingClient()
    client.document_pages.append(_page([]))
    sink = OneDocumentAtATimeSink()

    _synchronizer(client, sink).sync(materialized_documents())

    assert sink.bodies == [b"first", b"second"]


def test_sync_spool_failure_has_no_state_or_destination_activity(monkeypatch: pytest.MonkeyPatch) -> None:
    temporary_file = FailingTemporaryFile()
    monkeypatch.setattr(interface_module, "TemporaryFile", lambda **kwargs: temporary_file)
    client = FakeCcfClient()
    sink = FakeSink()

    with pytest.raises(RuntimeError) as caught:
        _synchronizer(client, sink).sync([make_document("doc-1")])

    assert str(caught.value) == "Failed to spool the connector snapshot."
    assert "private temporary path" not in str(caught.value)
    assert temporary_file.closed
    assert client.list_requests == []
    assert client.put_batches == []
    assert client.delete_batches == []
    assert sink.written == []
    assert sink.deleted == []


@pytest.mark.parametrize("destination_fails", [False, True])
def test_sync_closes_spool_on_success_and_failure(
    monkeypatch: pytest.MonkeyPatch,
    destination_fails: bool,
) -> None:
    created_files: list[object] = []
    actual_temporary_file = tempfile.TemporaryFile

    def recording_temporary_file(**kwargs: object) -> object:
        temporary_file = actual_temporary_file(**kwargs)
        created_files.append(temporary_file)
        return temporary_file

    monkeypatch.setattr(interface_module, "TemporaryFile", recording_temporary_file)
    client = FakeCcfClient()
    client.document_pages.append(_page([]))
    sink = FailingSink() if destination_fails else FakeSink()

    if destination_fails:
        with pytest.raises(RuntimeError, match="destination failed"):
            _synchronizer(client, sink).sync([make_document("doc-1")])
    else:
        _synchronizer(client, sink).sync([make_document("doc-1")])

    assert len(created_files) == 1
    assert created_files[0].closed  # type: ignore[attr-defined]
