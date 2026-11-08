"""The requests the `DocumentStore` takes and the values it returns.

Every request names documents and collections by the connector implementation's own source
IDs; the store turns them into the framework's `doc-` and `col-` IDs.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Annotated, BinaryIO

from ccf_api_models.identifiers import CollectionIdentity
from pydantic import (
    BaseModel,
    ConfigDict,
    SkipValidation,
    StrictInt,
    StringConstraints,
    ValidationError,
    field_validator,
)

from custom_connector_framework.store.access_control import AccessControl
from custom_connector_framework.store.metadata.bedrock import BedrockMetadataFile, BedrockValue, BedrockValueType
from custom_connector_framework.store.metadata.quick import QuickMetadataFile
from custom_connector_framework.store.metadata.s3 import S3MetadataFile

logger = logging.getLogger(__name__)


class StoreRequest(BaseModel):
    """A request the store validates before it reads content or writes to S3; unknown members fail."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)


class StoreValue(BaseModel):
    """A value the store returns."""

    model_config = ConfigDict(frozen=True)


class StringAttribute(StoreValue):
    value: str

    def to_bedrock(self) -> BedrockValue:
        return BedrockValue(type=BedrockValueType.STRING, string_value=self.value)

    def to_json(self) -> str | int | list[str]:
        return self.value


class NumberAttribute(StoreValue):
    """An integer attribute, which Bedrock range filters compare; store a time as epoch seconds."""

    value: StrictInt

    def to_bedrock(self) -> BedrockValue:
        return BedrockValue(type=BedrockValueType.NUMBER, number_value=self.value)

    def to_json(self) -> str | int | list[str]:
        return self.value


class StringListAttribute(StoreValue):
    """A list of text values."""

    values: tuple[str, ...]

    def to_bedrock(self) -> BedrockValue:
        return BedrockValue(type=BedrockValueType.STRING_LIST, string_list_value=self.values)

    def to_json(self) -> str | int | list[str]:
        return list(self.values)


type AttributeValue = StringAttribute | NumberAttribute | StringListAttribute

RESERVED_ATTRIBUTE_NAMES = (
    BedrockMetadataFile.RESERVED_NAMES | QuickMetadataFile.RESERVED_NAMES | S3MetadataFile.RESERVED_NAMES
)
"""Metadata members the store writes itself; an attribute can't take their names."""


class DocumentAttribute(StoreRequest):
    """One named metadata attribute of a document: a string, an integer, or a list of strings."""

    name: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    value: AttributeValue


type SourceId = Annotated[str, StringConstraints(min_length=1, max_length=1024)]
"""A source document or collection ID, 1 to 1,024 characters, as the framework API accepts."""
type FileName = Annotated[str, StringConstraints(min_length=1, max_length=1024, pattern=r"^[^/]+$")]
"""A file name with no `/`, so the stored file stays under the document's prefix."""


class SkipReason(StrEnum):
    """Why the store left a document out."""

    EMPTY_CONTENT = "empty_content"
    UNSUPPORTED_CONTENT = "unsupported_content"


class PutDocumentRequest(StoreRequest):
    """One document to store: its source ID, file name, content stream, and metadata."""

    document_id: SourceId
    file_name: FileName
    content: SkipValidation[BinaryIO]
    attributes: tuple[DocumentAttribute, ...] = ()
    collection_id: SourceId | None = None
    source_version: str | None = None
    media_type: str | None = None
    access_control: AccessControl | None = None
    title: str | None = None
    source_uri: str | None = None

    @field_validator("attributes")
    @classmethod
    def _unique_unreserved_names(cls, attributes: tuple[DocumentAttribute, ...]) -> tuple[DocumentAttribute, ...]:
        names = [attribute.name for attribute in attributes]
        reserved = sorted(RESERVED_ATTRIBUTE_NAMES.intersection(names))
        if reserved:
            raise ValueError(f"attribute names {reserved} are reserved for the store's own metadata")
        repeated = sorted({name for name in names if names.count(name) > 1})
        if repeated:
            raise ValueError(f"attribute names {repeated} appear more than once")
        return attributes


class DeleteDocumentRequest(StoreRequest):
    """A document the source no longer has."""

    document_id: SourceId
    collection_id: SourceId | None = None


class ListDocumentsRequest(StoreRequest):
    """The collection whose stored documents to list; None is the default collection."""

    collection_id: str | None = None


class StoredDocument(StoreValue):
    """What the framework holds for one document, by its source ID."""

    document_id: str
    source_version: str | None
    updated_at: float


class SourceDocument(StoreRequest):
    """A document the source has now, and its version."""

    document_id: str
    source_version: str | None = None


class DiffRequest(StoreRequest):
    """The documents a source has now in one collection."""

    current: tuple[SourceDocument, ...]
    collection_id: str | None = None


class DiffResponse(StoreValue):
    """Source document IDs that are new, have a different version, or are stored but no longer current."""

    new: tuple[str, ...]
    changed: tuple[str, ...]
    missing: tuple[str, ...]


class GetCheckpointRequest[CheckpointT: BaseModel](StoreRequest):
    """The collection whose checkpoint to read (None is the default collection) and the model it parses into."""

    checkpoint_type: type[CheckpointT]
    collection_id: str | None = None


class SaveCheckpointRequest(StoreRequest):
    """A collection's new checkpoint, within the generated `CheckpointData` limits in characters and UTF-8 bytes."""

    checkpoint: str
    collection_id: str | None = None


class StoredCollection(StoreValue):
    """A stored collection's source ID (None for the default collection) and checkpoint text."""

    collection_id: str | None
    checkpoint: str | None

    def parse_checkpoint[CheckpointT: BaseModel](self, checkpoint_type: type[CheckpointT]) -> CheckpointT | None:
        """The checkpoint as the model; none, or text the model rejects, is None, and rejected text is logged."""
        if self.checkpoint is None:
            return None
        try:
            return checkpoint_type.model_validate_json(self.checkpoint)
        except ValidationError:
            # Names the collection only: the checkpoint text can hold source data.
            logger.warning(
                "checkpoint_unreadable",
                extra={"collection_id": CollectionIdentity.of(self.collection_id).collection_id},
            )
            return None


class DeleteCollectionRequest(StoreRequest):
    """A collection to delete with all its documents."""

    collection_id: SourceId


class SkippedCounts(StoreValue):
    """How many documents the store left out: one member per `SkipReason`, named by its value."""

    empty_content: int = 0
    unsupported_content: int = 0


class ReportCounts(StoreValue):
    """What the store did for one collection, or for all of them."""

    written: int = 0
    unchanged: int = 0
    deleted: int = 0
    skipped: SkippedCounts = SkippedCounts()
    pending: int = 0


class CollectionReport(StoreValue):
    """One collection's counts, by its source collection ID (None for the default collection)."""

    collection_id: str | None
    counts: ReportCounts


class CollectionCounts(StoreValue):
    """How many collections the job wrote, left unchanged, and deleted.

    A collection is written when the store sent any record or checkpoint for it, unchanged when
    the implementation read its checkpoint and then sent nothing for it, and deleted when
    `delete_collection` removed it.
    """

    written: int = 0
    unchanged: int = 0
    deleted: int = 0


class StoreReport(StoreValue):
    """Each collection's counts, and the totals across them."""

    collections: tuple[CollectionReport, ...]
    total: ReportCounts
    collection_counts: CollectionCounts = CollectionCounts()
