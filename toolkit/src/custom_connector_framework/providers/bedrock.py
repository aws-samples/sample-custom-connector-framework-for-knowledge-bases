"""Amazon Bedrock Managed Knowledge Base (S3) adapter.

A Bedrock knowledge base with an S3 data source reads each document's metadata
from a companion file named `<document-key>.metadata.json` sitting next to the
document in the bucket. This adapter writes that file. It holds the document's
attributes under `metadataAttributes` and, when the document has an access
control list, the list of users allowed or denied under `accessControlList`.

The connector only writes to S3. Bedrock ingests the bucket on its own, so this
adapter never calls Bedrock; it just renders the sidecar.

Bedrock's S3 connector only understands user principals in an ACL, matched by
exact email address. A group principal has no representation, so the adapter
either raises or drops it, depending on how it is configured.

The format matches the Bedrock "Document-level access controls" page for S3 data
sources (`kb-managed-ds-s3-acl.html`), checked 2026-10-09.
"""

import json
from enum import Enum
from typing import Any, ClassVar

from pydantic import BaseModel

from custom_connector_framework.models.acl import AccessControl, PrincipalType
from custom_connector_framework.models.document import Document
from custom_connector_framework.models.exceptions import UnsupportedAclError
from custom_connector_framework.providers.base import RenderedDocument, SidecarFile
from custom_connector_framework.providers.supported_types import BEDROCK_SUPPORTED_EXTENSIONS, effective_suffix

_METADATA_SUFFIX = ".metadata.json"


class AclUnsupportedGroupPolicy(str, Enum):
    """How to handle a GROUP principal, which Bedrock's S3 connector cannot represent."""

    DROP = "DROP"
    RAISE = "RAISE"


class BedrockMetadataValueType(str, Enum):
    """Bedrock `metadataAttributes` value types."""

    STRING = "STRING"
    NUMBER = "NUMBER"
    BOOLEAN = "BOOLEAN"
    STRING_LIST = "STRING_LIST"


class BedrockAccessControlEntry(BaseModel):
    """One entry in a Bedrock S3 `accessControlList`."""

    Name: str
    Type: str = "USER"
    Access: str


def _metadata_value(value: str) -> dict[str, Any]:
    return {"value": {"type": BedrockMetadataValueType.STRING.value, "stringValue": value}}


class BedrockKnowledgeBaseAdapter:
    """Renders Bedrock S3 data source metadata and ACL sidecars."""

    fingerprint: ClassVar[str] = "bedrock:1"
    _ATTRIBUTE_FIELD_NAMES: ClassVar[dict[str, str]] = {
        "source_uri": "source_uri",
        "created_at": "created_at",
        "last_updated_at": "last_updated_at",
    }

    def __init__(
        self,
        unsupported_group_policy: AclUnsupportedGroupPolicy = AclUnsupportedGroupPolicy.RAISE,
    ) -> None:
        self._unsupported_group_policy = unsupported_group_policy

    def object_suffix(self, original_suffix: str) -> str:
        """Rewrite an unsupported extension to `.txt` so Bedrock ingests it."""
        return effective_suffix(original_suffix, BEDROCK_SUPPORTED_EXTENSIONS)

    def render_document(self, document: Document, object_key: str) -> RenderedDocument:
        """Build the `<object_key>.metadata.json` sidecar, including the per-document ACL."""
        acl_entries = self._render_acl(document.metadata.access_control)

        metadata_attributes: dict[str, Any] = {}
        for field_name, bedrock_name in self._ATTRIBUTE_FIELD_NAMES.items():
            value = getattr(document.metadata, field_name)
            if value is not None:
                metadata_attributes[bedrock_name] = _metadata_value(value)
        metadata_attributes["title"] = _metadata_value(document.metadata.title)
        for name, value in document.metadata.attributes.items():
            metadata_attributes[name] = _metadata_value(value)

        body: dict[str, Any] = {"metadataAttributes": metadata_attributes}
        if acl_entries:
            body["accessControlList"] = [entry.model_dump() for entry in acl_entries]

        sidecar = SidecarFile(key=f"{object_key}{_METADATA_SUFFIX}", content=json.dumps(body).encode("utf-8"))
        return RenderedDocument(sidecar=sidecar)

    def _render_acl(self, access_control: AccessControl) -> list[BedrockAccessControlEntry]:
        entries: list[BedrockAccessControlEntry] = []
        for entry in access_control.entries:
            if entry.principal_type is PrincipalType.GROUP:
                if self._unsupported_group_policy is AclUnsupportedGroupPolicy.RAISE:
                    raise UnsupportedAclError(
                        "Bedrock's S3 connector ACLs do not support GROUP principals. "
                        "Resolve the group to its member emails, or construct the adapter "
                        "with unsupported_group_policy=DROP."
                    )
                continue
            entries.append(BedrockAccessControlEntry(Name=entry.principal_id, Access=entry.access.value))
        return entries
