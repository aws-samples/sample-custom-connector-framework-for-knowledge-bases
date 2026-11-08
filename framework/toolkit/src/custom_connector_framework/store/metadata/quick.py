"""The metadata file Amazon Quick reads beside each document in an S3 data source.

Quick reads `<file>.metadata.json` with `DocumentId`, `Attributes`, `Title`, `ContentType`,
and an `AccessControlList` that may name users and groups. The tenant turns access control
lists on when it connects Quick to the bucket, so the store writes a document's list
whenever it has one. The content types and their extensions follow Quick's "Supported
document types" page (checked 2026-10-09).
"""

from __future__ import annotations

from enum import Enum
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from custom_connector_framework.store.access_control import AccessEffect, PrincipalType
from custom_connector_framework.store.metadata.canonical_json import CanonicalJsonFile

type QuickValue = str | int | list[str]


class QuickContentType(str, Enum):
    """Quick's `ContentType` values."""

    PDF = "PDF"
    HTML = "HTML"
    XML = "XML"
    XSLT = "XSLT"
    MD = "MD"
    CSV = "CSV"
    MS_EXCEL = "MS_EXCEL"
    JSON = "JSON"
    RTF = "RTF"
    PPT = "PPT"
    MS_WORD = "MS_WORD"
    PLAIN_TEXT = "PLAIN_TEXT"


CONTENT_TYPE_BY_EXTENSION = {
    "pdf": QuickContentType.PDF,
    "html": QuickContentType.HTML,
    "xml": QuickContentType.XML,
    "xslt": QuickContentType.XSLT,
    "md": QuickContentType.MD,
    "csv": QuickContentType.CSV,
    "xls": QuickContentType.MS_EXCEL,
    "xlsx": QuickContentType.MS_EXCEL,
    "json": QuickContentType.JSON,
    "rtf": QuickContentType.RTF,
    "ppt": QuickContentType.PPT,
    "pptx": QuickContentType.PPT,
    "doc": QuickContentType.MS_WORD,
    "docx": QuickContentType.MS_WORD,
    "txt": QuickContentType.PLAIN_TEXT,
}
"""The extensions Quick ingests, and each one's content type."""


class QuickOwnAttribute(str, Enum):
    """The `Attributes` the store writes itself; `_source_uri` is Quick's reserved name for the source URL."""

    DOCUMENT_ARN = "document_arn"
    SOURCE_URI = "_source_uri"


class QuickAccessControlEntry(BaseModel):
    """One entry in Quick's `AccessControlList`."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(serialization_alias="Name")
    type: PrincipalType = Field(serialization_alias="Type")
    access: AccessEffect = Field(serialization_alias="Access")


class QuickMetadataFile(CanonicalJsonFile):
    RESERVED_NAMES: ClassVar[frozenset[str]] = frozenset(name.value for name in QuickOwnAttribute)

    document_id: str = Field(serialization_alias="DocumentId")
    title: str = Field(serialization_alias="Title")
    content_type: QuickContentType = Field(serialization_alias="ContentType")
    attributes: dict[str, QuickValue] = Field(serialization_alias="Attributes")
    access_control_list: tuple[QuickAccessControlEntry, ...] | None = Field(
        default=None, serialization_alias="AccessControlList"
    )
    """Written only when the document's list has entries."""
