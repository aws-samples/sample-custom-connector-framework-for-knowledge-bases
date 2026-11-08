"""Each destination type's format: the file types it ingests, its metadata file, and its access-control rule.

`DestinationFormats.select` chooses the format once per job from the destination's type. The
Bedrock extensions follow Bedrock's "Supported document formats and limits" page
(`knowledge-base-ds.html`, checked 2026-10-09); Quick's are in `metadata/quick.py`.
"""

from __future__ import annotations

from enum import Enum
from pathlib import PurePosixPath
from typing import ClassVar, Protocol

from ccf_api_models.models import Destination, DestinationType
from pydantic import BaseModel, ConfigDict

from custom_connector_framework.store.access_control import AccessControl, PrincipalType, UnsupportedAccessControlError
from custom_connector_framework.store.media_types import SupportedTypes
from custom_connector_framework.store.metadata.bedrock import (
    BedrockAccessControlEntry,
    BedrockAttribute,
    BedrockMetadataFile,
    BedrockOwnAttribute,
)
from custom_connector_framework.store.metadata.quick import (
    CONTENT_TYPE_BY_EXTENSION,
    QuickAccessControlEntry,
    QuickContentType,
    QuickMetadataFile,
    QuickOwnAttribute,
)
from custom_connector_framework.store.metadata.s3 import S3MetadataFile, S3OwnAttribute
from custom_connector_framework.store.models import PutDocumentRequest, StringAttribute


class AccessControlUse(str, Enum):
    """What the destination does with a document's access control list."""

    NONE = "none"
    """The document has no list, or the destination keeps no lists."""
    WRITTEN = "written"
    LEFT_OUT = "left_out"
    """The destination has access control lists turned off, so the document's list isn't written."""


class MetadataFileRequest(BaseModel):
    """One document to describe: its put request, framework document ID and ARN, and stored file name."""

    model_config = ConfigDict(frozen=True)

    request: PutDocumentRequest
    document_id: str
    document_arn: str
    stored_name: str

    @property
    def title(self) -> str:
        return self.request.title or self.request.file_name

    def json_attributes(self) -> dict[str, str | int | list[str]]:
        """The document's attributes as JSON values, by name."""
        return {attribute.name: attribute.value.to_json() for attribute in self.request.attributes}


class DestinationFormat(Protocol):
    """How one destination type stores a document."""

    @property
    def supported_types(self) -> SupportedTypes: ...

    def check_access_control(self, access_control: AccessControl | None) -> AccessControlUse:
        """Return what the destination does with the list; raise `UnsupportedAccessControlError` if it can't."""
        ...

    def metadata_file(self, request: MetadataFileRequest) -> bytes: ...


class BedrockFormat:
    """A Bedrock knowledge base's S3 data source, which matches users by email and has no groups."""

    SUPPORTED_TYPES: ClassVar[SupportedTypes] = SupportedTypes(
        any_type=False, extensions=frozenset({"txt", "md", "html", "doc", "docx", "csv", "xls", "xlsx", "pdf"})
    )

    def __init__(self, access_control_enabled: bool) -> None:
        self._access_control_enabled = access_control_enabled

    @property
    def supported_types(self) -> SupportedTypes:
        return self.SUPPORTED_TYPES

    def check_access_control(self, access_control: AccessControl | None) -> AccessControlUse:
        if access_control is None or access_control.is_empty():
            return AccessControlUse.NONE
        if not self._access_control_enabled:
            return AccessControlUse.LEFT_OUT
        if any(entry.principal_type is PrincipalType.GROUP for entry in access_control.entries):
            raise UnsupportedAccessControlError(
                "A Bedrock data source's access control lists name users only; resolve each group to its users"
            )
        return AccessControlUse.WRITTEN

    def metadata_file(self, request: MetadataFileRequest) -> bytes:
        document = request.request
        own = {
            BedrockOwnAttribute.DOCUMENT_ARN.value: request.document_arn,
            BedrockOwnAttribute.TITLE.value: request.title,
            BedrockOwnAttribute.SOURCE_URI.value: document.source_uri,
        }
        access_control = document.access_control
        entries = None
        if access_control is not None and self.check_access_control(access_control) is AccessControlUse.WRITTEN:
            entries = tuple(
                BedrockAccessControlEntry(name=entry.principal_id, access=entry.access)
                for entry in access_control.entries
            )
        return BedrockMetadataFile(
            metadata_attributes={
                **{
                    attribute.name: BedrockAttribute(value=attribute.value.to_bedrock())
                    for attribute in document.attributes
                },
                **{
                    name: BedrockAttribute(value=StringAttribute(value=value).to_bedrock())
                    for name, value in own.items()
                    if value is not None
                },
            },
            access_control_list=entries,
        ).to_json_bytes()


class QuickFormat:
    """An Amazon Quick S3 data source, which names users and groups in a document's list."""

    SUPPORTED_TYPES: ClassVar[SupportedTypes] = SupportedTypes(
        any_type=False, extensions=frozenset(CONTENT_TYPE_BY_EXTENSION)
    )

    @property
    def supported_types(self) -> SupportedTypes:
        return self.SUPPORTED_TYPES

    def check_access_control(self, access_control: AccessControl | None) -> AccessControlUse:
        if access_control is None or access_control.is_empty():
            return AccessControlUse.NONE
        return AccessControlUse.WRITTEN

    def metadata_file(self, request: MetadataFileRequest) -> bytes:
        document = request.request
        own = {QuickOwnAttribute.DOCUMENT_ARN.value: request.document_arn}
        if document.source_uri is not None:
            own[QuickOwnAttribute.SOURCE_URI.value] = document.source_uri
        access_control = document.access_control
        entries = None
        if access_control is not None and not access_control.is_empty():
            entries = tuple(
                QuickAccessControlEntry(name=entry.principal_id, type=entry.principal_type, access=entry.access)
                for entry in access_control.entries
            )
        extension = PurePosixPath(request.stored_name).suffix.lower().lstrip(".")
        return QuickMetadataFile(
            document_id=request.document_id,
            title=request.title,
            content_type=CONTENT_TYPE_BY_EXTENSION.get(extension, QuickContentType.PLAIN_TEXT),
            attributes={**request.json_attributes(), **own},
            access_control_list=entries,
        ).to_json_bytes()


class S3Format:
    """A plain bucket: every file type, and no access control list, because IAM governs the bucket."""

    SUPPORTED_TYPES: ClassVar[SupportedTypes] = SupportedTypes(any_type=True)

    @property
    def supported_types(self) -> SupportedTypes:
        return self.SUPPORTED_TYPES

    def check_access_control(self, access_control: AccessControl | None) -> AccessControlUse:
        return AccessControlUse.NONE

    def metadata_file(self, request: MetadataFileRequest) -> bytes:
        document = request.request
        own = {S3OwnAttribute.DOCUMENT_ARN.value: request.document_arn}
        if document.source_uri is not None:
            own[S3OwnAttribute.SOURCE_URI.value] = document.source_uri
        return S3MetadataFile(title=request.title, attributes={**request.json_attributes(), **own}).to_json_bytes()


class DestinationFormats:
    """Chooses a destination's format."""

    def select(self, destination: Destination) -> DestinationFormat:
        match destination.type:
            case DestinationType.BEDROCK:
                return BedrockFormat(destination.access_control_enabled)
            case DestinationType.QUICK:
                return QuickFormat()
            case DestinationType.S3:
                return S3Format()
