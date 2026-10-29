"""Write in-memory documents and provider sidecars to an object store."""

import hashlib
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from custom_connector_framework.models.document import Document
from custom_connector_framework.models.exceptions import SinkDeleteError, SinkUploadError
from custom_connector_framework.object_store import DeleteObjectRequest, ObjectStore, PutObjectRequest
from custom_connector_framework.providers.base import KnowledgeBaseProviderAdapter


class SinkConfig(BaseModel):
    """Where the runtime writes documents within the service object store."""

    model_config = ConfigDict(frozen=True)

    prefix: str


class DocumentWriteResult(BaseModel):
    """Exact immutable keys written for one document."""

    model_config = ConfigDict(frozen=True)

    object_key: str
    sidecar_key: str | None = None


class DocumentDeleteRequest(BaseModel):
    """Keys to delete, with optional keys preserved after a replacement write."""

    model_config = ConfigDict(frozen=True)

    object_key: str
    sidecar_key: str | None = None
    preserve_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class _DocumentWriteRequest:
    document: Document
    protected_object_keys: tuple[str, ...] = ()


class DocumentSink:
    """Manage document and provider-sidecar objects under one key prefix."""

    def __init__(self, object_store: ObjectStore, config: SinkConfig, adapter: KnowledgeBaseProviderAdapter) -> None:
        self._object_store = object_store
        self._config = config
        self._adapter = adapter

    def _reconciliation_checksum(self, document_checksum: str) -> str:
        """Bind persisted state to document bytes and renderer revision."""
        identity = f"{document_checksum}\0{self._adapter.fingerprint}".encode()
        return hashlib.sha256(identity).hexdigest()

    def object_key_for(self, document: Document) -> str:
        """Return the revision-specific object key for a document."""
        safe_id = document.id.strip("/")
        checksum = document.get_checksum()
        suffix = self._adapter.object_suffix(document.suffix)
        prefix = self._config.prefix.strip("/")
        revision_key = f"{safe_id}/{checksum}{suffix}"
        return f"{prefix}/{revision_key}" if prefix else revision_key

    def put(self, request: _DocumentWriteRequest) -> DocumentWriteResult:
        """Write document content and an optional sidecar, returning exact keys."""
        document = request.document
        object_key = self.object_key_for(document)
        rendered = self._adapter.render_document(document, object_key)

        try:
            self._object_store.put_object(PutObjectRequest(key=object_key, body=document.content))
        except Exception:
            raise SinkUploadError("Failed to upload a document to the object store.") from None

        if rendered.sidecar is not None:
            try:
                self._object_store.put_object(PutObjectRequest(key=rendered.sidecar.key, body=rendered.sidecar.content))
            except Exception:
                if object_key not in request.protected_object_keys:
                    try:
                        self._object_store.delete_object(DeleteObjectRequest(key=object_key))
                    except Exception:
                        raise SinkUploadError(
                            "Failed to upload a document sidecar, and incomplete-object cleanup failed."
                        ) from None
                raise SinkUploadError("Failed to upload a document to the object store.") from None

        return DocumentWriteResult(
            object_key=object_key,
            sidecar_key=rendered.sidecar.key if rendered.sidecar is not None else None,
        )

    def delete(self, request: DocumentDeleteRequest) -> None:
        """Delete exact tracked document and sidecar keys."""
        try:
            for key in (request.object_key, request.sidecar_key):
                if key is not None and key not in request.preserve_keys:
                    self._object_store.delete_object(DeleteObjectRequest(key=key))
        except Exception:
            raise SinkDeleteError("Failed to delete a document from the object store.") from None
