"""Provider adapter contract: render a document's metadata sidecar."""

from typing import ClassVar, Protocol

from pydantic import BaseModel, ConfigDict

from custom_connector_framework.models.document import Document


class SidecarFile(BaseModel):
    """The metadata file a provider writes next to a document in the store."""

    model_config = ConfigDict(frozen=True)

    key: str
    content: bytes


class RenderedDocument(BaseModel):
    """What a provider needs written for one document.

    ``sidecar`` is the single metadata file to write alongside the document, or
    ``None`` when the provider needs none (plain S3 writes no sidecar).
    """

    model_config = ConfigDict(frozen=True)

    sidecar: SidecarFile | None = None


class KnowledgeBaseProviderAdapter(Protocol):
    """Renders a document's provider sidecar."""

    fingerprint: ClassVar[str]

    def object_suffix(self, original_suffix: str) -> str:
        """Return the file suffix to store a document under for this provider.

        Unsupported extensions are rewritten (usually by appending ``.txt``) so
        the provider ingests the content; supported extensions are unchanged.
        """
        ...

    def render_document(self, document: Document, object_key: str) -> RenderedDocument:
        """Build the sidecar for one document, keyed to its S3 object key."""
        ...
