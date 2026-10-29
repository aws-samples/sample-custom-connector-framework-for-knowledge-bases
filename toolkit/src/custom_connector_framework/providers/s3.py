"""Plain S3 adapter: store document bytes only, with no managed-KB metadata or ACLs.

Used for `Provider.S3`, where no managed knowledge base reads the bucket. The
adapter writes no metadata sidecar and no ACL file. Access to the stored objects
is governed by the bucket's IAM and bucket policy.
"""

from typing import ClassVar

from custom_connector_framework.models.document import Document
from custom_connector_framework.providers.base import RenderedDocument


class S3Adapter:
    """Writes document content to S3 with no sidecar and no ACL handling."""

    fingerprint: ClassVar[str] = "s3:1"

    def object_suffix(self, original_suffix: str) -> str:
        """Return the suffix unchanged: plain S3 has no managed KB to satisfy."""
        return original_suffix

    def render_document(self, document: Document, object_key: str) -> RenderedDocument:  # noqa: ARG002
        """Return no sidecar: only the document object itself is written."""
        return RenderedDocument(sidecar=None)
