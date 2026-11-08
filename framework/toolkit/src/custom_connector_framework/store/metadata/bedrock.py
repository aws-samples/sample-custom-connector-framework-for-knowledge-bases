"""The metadata file an Amazon Bedrock knowledge base reads beside each document in an S3 data source.

Bedrock reads `<file>.metadata.json`: attributes under `metadataAttributes`, and, for a data
source with access control lists turned on, the users allowed or denied under
`accessControlList`. The format follows Bedrock's "Document-level access controls" page for
S3 data sources (`kb-managed-ds-s3-acl.html`, checked 2026-10-09). Bedrock's S3 connector
matches users by exact email and has no group principal.
"""

from __future__ import annotations

from enum import Enum
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from custom_connector_framework.store.access_control import AccessEffect, PrincipalType
from custom_connector_framework.store.metadata.canonical_json import CanonicalJsonFile


class BedrockValueType(str, Enum):
    """Bedrock `metadataAttributes` value types."""

    STRING = "STRING"
    NUMBER = "NUMBER"
    STRING_LIST = "STRING_LIST"


class BedrockValue(BaseModel):
    """One typed value; exactly the member that matches `type` is set."""

    model_config = ConfigDict(frozen=True)

    type: BedrockValueType
    string_value: str | None = Field(default=None, serialization_alias="stringValue")
    number_value: int | None = Field(default=None, serialization_alias="numberValue")
    string_list_value: tuple[str, ...] | None = Field(default=None, serialization_alias="stringListValue")


class BedrockAttribute(BaseModel):
    """One entry of `metadataAttributes`."""

    model_config = ConfigDict(frozen=True)

    value: BedrockValue


class BedrockAccessControlEntry(BaseModel):
    """One entry in Bedrock's `accessControlList`; Bedrock's S3 connector names users only."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(serialization_alias="Name")
    type: PrincipalType = Field(default=PrincipalType.USER, serialization_alias="Type")
    access: AccessEffect = Field(serialization_alias="Access")


class BedrockOwnAttribute(str, Enum):
    """The `metadataAttributes` the store writes itself."""

    DOCUMENT_ARN = "document_arn"
    TITLE = "title"
    SOURCE_URI = "source_uri"


class BedrockMetadataFile(CanonicalJsonFile):
    RESERVED_NAMES: ClassVar[frozenset[str]] = frozenset(name.value for name in BedrockOwnAttribute)

    metadata_attributes: dict[str, BedrockAttribute] = Field(serialization_alias="metadataAttributes")
    access_control_list: tuple[BedrockAccessControlEntry, ...] | None = Field(
        default=None, serialization_alias="accessControlList"
    )
    """Written only for a destination with access control lists turned on, and only when the list has entries."""
