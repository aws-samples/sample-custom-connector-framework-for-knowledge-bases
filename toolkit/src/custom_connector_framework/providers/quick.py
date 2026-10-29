"""Amazon Quick Knowledge Base S3 metadata adapter.

Amazon Quick Index crawls an S3 data source and reads a companion metadata file
for each document. This adapter writes `<document-key>.metadata.json` next to
the document with `Attributes`, `AccessControlList`, `Title`, and `ContentType`
fields. The access control list may name both user and group principals.
"""

import json
from typing import ClassVar

from pydantic import BaseModel

from custom_connector_framework.models.acl import AccessControl
from custom_connector_framework.models.document import Document
from custom_connector_framework.models.exceptions import UnsupportedDocumentError
from custom_connector_framework.providers.base import RenderedDocument, SidecarFile
from custom_connector_framework.providers.supported_types import QUICK_SUPPORTED_EXTENSIONS, effective_suffix
from custom_connector_framework.types import ContentType

_METADATA_SUFFIX = ".metadata.json"


class QuickAccessControlEntry(BaseModel):
    """One entry in an Amazon Quick `AccessControlList`."""

    Name: str
    Type: str
    Access: str


def _render_acl(access_control: AccessControl) -> list[dict[str, str]]:
    return [
        QuickAccessControlEntry(
            Name=entry.principal_id,
            Type=entry.principal_type.value,
            Access=entry.access.value,
        ).model_dump()
        for entry in access_control.entries
    ]


class QuickKnowledgeBaseAdapter:
    """Renders Amazon Quick metadata sidecars for an S3 data source."""

    fingerprint: ClassVar[str] = "quick:1"
    _ATTRIBUTE_FIELD_NAMES: ClassVar[dict[str, str]] = {
        "source_uri": "_source_uri",
        "created_at": "_created_at",
        "last_updated_at": "_last_updated_at",
    }

    def object_suffix(self, original_suffix: str) -> str:
        """Rewrite an unsupported extension to `.txt` so Amazon Quick ingests it."""
        return effective_suffix(original_suffix, QUICK_SUPPORTED_EXTENSIONS)

    def render_document(self, document: Document, object_key: str) -> RenderedDocument:
        """Build the `<object_key>.metadata.json` sidecar for a Quick S3 data source."""
        try:
            content_type = document.infer_content_type()
        except UnsupportedDocumentError:
            content_type = ContentType.PLAIN_TEXT
        attributes: dict[str, str] = {}
        for field_name, quick_name in self._ATTRIBUTE_FIELD_NAMES.items():
            value = getattr(document.metadata, field_name)
            if value is not None:
                attributes[quick_name] = value
        attributes.update(document.metadata.attributes)

        body = {
            "DocumentId": document.id,
            "Attributes": attributes,
            "AccessControlList": _render_acl(document.metadata.access_control),
            "Title": document.metadata.title,
            "ContentType": content_type.value,
        }
        sidecar = SidecarFile(key=f"{object_key}{_METADATA_SUFFIX}", content=json.dumps(body).encode("utf-8"))
        return RenderedDocument(sidecar=sidecar)
