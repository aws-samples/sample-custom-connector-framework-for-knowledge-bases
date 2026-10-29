"""Tests for in-memory document writes and exact-key deletes."""

import json
from pathlib import Path

import boto3
import pytest
from pydantic import ValidationError

from custom_connector_framework.models.exceptions import SinkUploadError
from custom_connector_framework.object_store import (
    DeleteObjectRequest,
    LocalFileObjectStore,
    ObjectStore,
    PutObjectRequest,
    S3ObjectStore,
)
from custom_connector_framework.providers.bedrock import BedrockKnowledgeBaseAdapter
from custom_connector_framework.providers.quick import QuickKnowledgeBaseAdapter
from custom_connector_framework.sink import (
    DocumentDeleteRequest,
    DocumentSink,
    DocumentWriteResult,
    SinkConfig,
    _DocumentWriteRequest,
)
from tests.conftest import make_document


def _sink(object_store: ObjectStore, prefix: str = "connectors/cc-1") -> DocumentSink:
    return DocumentSink(
        object_store=object_store,
        config=SinkConfig(prefix=prefix),
        adapter=QuickKnowledgeBaseAdapter(),
    )


class SidecarFailureObjectStore:
    def __init__(self, cleanup_fails: bool, fail_on_put: int = 2) -> None:
        self.cleanup_fails = cleanup_fails
        self.fail_on_put = fail_on_put
        self.put_count = 0
        self.deleted_keys: list[str] = []
        self.objects: dict[str, bytes] = {}

    def put_object(self, request: PutObjectRequest) -> None:
        self.put_count += 1
        if self.put_count == self.fail_on_put:
            raise RuntimeError("sensitive sidecar failure")
        self.objects[request.key] = request.body

    def delete_object(self, request: DeleteObjectRequest) -> None:
        self.deleted_keys.append(request.key)
        if self.cleanup_fails:
            raise RuntimeError("sensitive cleanup failure")
        self.objects.pop(request.key, None)


@pytest.mark.parametrize("key", ["/absolute/file", "../file", "repo/../file", "repo/path/.."])
def test_object_requests_reject_absolute_and_parent_traversal_keys(key: str) -> None:
    with pytest.raises(ValidationError):
        PutObjectRequest(key=key, body=b"content")
    with pytest.raises(ValidationError):
        DeleteObjectRequest(key=key)


def test_object_key_supports_empty_or_service_prefix(tmp_path: Path) -> None:
    document = make_document("doc-1")
    revision = f"doc-1/{document.get_checksum()}.txt"

    assert _sink(LocalFileObjectStore(tmp_path)).object_key_for(document) == f"connectors/cc-1/{revision}"
    assert _sink(LocalFileObjectStore(tmp_path), prefix="").object_key_for(document) == revision


def test_object_key_changes_with_content_or_metadata() -> None:
    sink = _sink(SidecarFailureObjectStore(cleanup_fails=False))
    original = make_document("doc-1")
    content_update = make_document("doc-1", content=b"updated")
    metadata_update = make_document("doc-1", title="Updated title")

    keys = {sink.object_key_for(document) for document in (original, content_update, metadata_update)}

    assert len(keys) == 3
    updated_documents = (original, content_update, metadata_update)
    assert all(document.get_checksum() in sink.object_key_for(document) for document in updated_documents)


def test_put_writes_document_and_sidecar_to_s3(s3_client: boto3.client, s3_bucket: str) -> None:
    sink = _sink(S3ObjectStore(s3_client, s3_bucket))

    document = make_document("doc-1")
    write_result = sink.put(_DocumentWriteRequest(document=document))
    expected_object_key = f"connectors/cc-1/doc-1/{document.get_checksum()}.txt"

    assert write_result.object_key == expected_object_key
    assert write_result.sidecar_key == f"{expected_object_key}.metadata.json"
    assert s3_client.get_object(Bucket=s3_bucket, Key=write_result.object_key)["Body"].read() == b"hello world"
    sidecar = s3_client.get_object(Bucket=s3_bucket, Key=write_result.sidecar_key)["Body"].read()
    assert json.loads(sidecar)["DocumentId"] == "doc-1"


def test_put_writes_from_memory_to_local_store(tmp_path: Path) -> None:
    root = tmp_path / "store"

    write_result = _sink(LocalFileObjectStore(root)).put(
        _DocumentWriteRequest(document=make_document("doc-1", b"in-memory"))
    )

    assert (root / write_result.object_key).read_bytes() == b"in-memory"
    assert write_result.sidecar_key is not None
    assert (root / write_result.sidecar_key).exists()


def test_put_wraps_store_failure_without_sensitive_details(s3_client: boto3.client) -> None:
    with pytest.raises(SinkUploadError) as caught:
        _sink(S3ObjectStore(s3_client, "does-not-exist-bucket")).put(
            _DocumentWriteRequest(document=make_document("doc-1"))
        )

    assert str(caught.value) == "Failed to upload a document to the object store."
    assert caught.value.__cause__ is None


def test_put_removes_document_when_sidecar_write_fails() -> None:
    store = SidecarFailureObjectStore(cleanup_fails=False)
    document = make_document("doc-1")
    sink = _sink(store)
    object_key = sink.object_key_for(document)

    with pytest.raises(SinkUploadError) as caught:
        sink.put(_DocumentWriteRequest(document=document))

    assert str(caught.value) == "Failed to upload a document to the object store."
    assert caught.value.__cause__ is None
    assert store.deleted_keys == [object_key]
    assert object_key not in store.objects


def test_put_reports_when_sidecar_write_and_body_cleanup_fail() -> None:
    store = SidecarFailureObjectStore(cleanup_fails=True)
    document = make_document("doc-1")
    sink = _sink(store)
    object_key = sink.object_key_for(document)

    with pytest.raises(SinkUploadError) as caught:
        sink.put(_DocumentWriteRequest(document=document))

    assert str(caught.value) == "Failed to upload a document sidecar, and incomplete-object cleanup failed."
    assert caught.value.__cause__ is None
    assert store.deleted_keys == [object_key]
    assert store.objects[object_key] == document.content


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_failed_sidecar_write_leaves_prior_revision_untouched(cleanup_fails: bool) -> None:
    store = SidecarFailureObjectStore(cleanup_fails=cleanup_fails, fail_on_put=4)
    sink = _sink(store)
    prior_document = make_document("doc-1", content=b"prior")
    prior_write = sink.put(_DocumentWriteRequest(document=prior_document))
    updated_document = make_document("doc-1", content=b"updated")
    updated_object_key = sink.object_key_for(updated_document)

    with pytest.raises(SinkUploadError):
        sink.put(_DocumentWriteRequest(document=updated_document))

    assert updated_object_key != prior_write.object_key
    assert store.objects[prior_write.object_key] == prior_document.content
    assert prior_write.sidecar_key in store.objects
    assert (updated_object_key in store.objects) is cleanup_fails
    assert store.deleted_keys == [updated_object_key]


def test_put_rewrites_java_for_managed_provider(tmp_path: Path) -> None:
    root = tmp_path / "store"
    sink = DocumentSink(
        object_store=LocalFileObjectStore(root),
        config=SinkConfig(prefix="connectors/cc-1"),
        adapter=BedrockKnowledgeBaseAdapter(),
    )

    document = make_document("App", b"class App {}", filename="App.java")
    write_result = sink.put(_DocumentWriteRequest(document=document))

    assert write_result.object_key == f"connectors/cc-1/App/{document.get_checksum()}.java.txt"
    assert (root / write_result.object_key).read_bytes() == b"class App {}"
    assert write_result.sidecar_key is not None
    assert (root / write_result.sidecar_key).exists()


def test_sink_models_only_carry_exact_keys() -> None:
    assert set(DocumentWriteResult.model_fields) == {"object_key", "sidecar_key"}
    assert set(DocumentDeleteRequest.model_fields) == {"object_key", "sidecar_key", "preserve_keys"}


def test_delete_uses_exact_keys_and_preserves_replacement_keys(tmp_path: Path) -> None:
    root = tmp_path / "store"
    store = LocalFileObjectStore(root)
    sink = _sink(store)
    for key in ("old/document.java", "old/document.java.metadata.json", "new/document.java.txt"):
        store.put_object(PutObjectRequest(key=key, body=b"content"))

    sink.delete(
        DocumentDeleteRequest(
            object_key="old/document.java",
            sidecar_key="old/document.java.metadata.json",
            preserve_keys=("new/document.java.txt",),
        )
    )

    assert not (root / "old/document.java").exists()
    assert not (root / "old/document.java.metadata.json").exists()
    assert (root / "new/document.java.txt").exists()
