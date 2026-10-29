"""In-memory document content and metadata for a connector snapshot.

Checksum data tracks identity, filename, supplied metadata, and content. This
lets reconciliation find provider-visible changes without generated timestamps.
"""

import hashlib
import json
import mimetypes
from pathlib import PurePath

import filetype
from pydantic import BaseModel, ConfigDict, Field

from custom_connector_framework.models.acl import AccessControl
from custom_connector_framework.models.exceptions import UnsupportedDocumentError
from custom_connector_framework.types import ContentType, DocumentTypeMapper


class DocumentMetadata(BaseModel):
    """Metadata associated with a document, including attributes and ACL."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str
    source_uri: str | None = None
    last_updated_at: str | None = None
    created_at: str | None = None
    attributes: dict[str, str] = Field(default_factory=dict)
    access_control: AccessControl = Field(default_factory=AccessControl)


class Document(BaseModel):
    """One fully materialized document in a connector snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: str
    content: bytes = Field(repr=False)
    filename: str
    metadata: DocumentMetadata

    @classmethod
    def from_bytes(
        cls,
        *,
        document_id: str,
        content: bytes,
        filename: str,
        metadata: DocumentMetadata,
    ) -> Document:
        """Create a document from bytes already fetched from the source."""
        return cls(id=document_id, content=content, filename=filename, metadata=metadata)

    @property
    def suffix(self) -> str:
        """Return the final filename suffix used by provider adapters."""
        return PurePath(self.filename).suffix

    def infer_content_type(self) -> ContentType:
        """Determine content type from bytes, MIME mapping, or filename suffix."""
        kind = filetype.guess(self.content)
        if kind is not None:
            try:
                return DocumentTypeMapper.get_content_type_from_mime(kind.mime)
            except UnsupportedDocumentError:
                pass

        mime_type, _ = mimetypes.guess_type(self.filename)
        if mime_type is not None:
            try:
                return DocumentTypeMapper.get_content_type_from_mime(mime_type)
            except UnsupportedDocumentError:
                pass

        try:
            return DocumentTypeMapper.get_content_type_from_extension(self.suffix)
        except (UnsupportedDocumentError, ValueError) as error:
            raise UnsupportedDocumentError("Unable to determine document content type.") from error

    def get_checksum(self) -> str:
        """Compute a stable SHA-256 checksum over identity, metadata, and content."""
        metadata = json.dumps(
            json.loads(self.metadata.model_dump_json(exclude_none=True)),
            sort_keys=True,
        )
        content_checksum = hashlib.sha256(self.content).hexdigest()
        checksum_input = f"{self.id}+{self.filename}+{metadata}+{content_checksum}"
        return hashlib.sha256(checksum_input.encode()).hexdigest()
