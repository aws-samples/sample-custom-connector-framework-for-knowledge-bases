"""Writes a document's metadata file and file under its prefix, skipping a file whose content is unchanged.

The source-access role may list only the custom connector's prefix, and S3 answers a
`HeadObject` of a key it can't list with 403 rather than 404, so the writer lists the
document's prefix first and reads the hash only of a listed key. The metadata file is
written before the file, and any other key under the prefix (a file of the document's old
name) is deleted after both.
"""

from __future__ import annotations

import io
from enum import StrEnum

from ccf_api_models.identifiers import METADATA_SUFFIX
from pydantic import BaseModel, ConfigDict

from custom_connector_framework.store.content_reader import SpooledContent
from custom_connector_framework.store.destination_objects import DestinationObjects, FileDeleteError, PutObjectRequest


class WriteOutcome(StrEnum):
    WRITTEN = "WRITTEN"
    UNCHANGED = "UNCHANGED"


class WriteFileRequest(BaseModel):
    """One document's prefix, stored file name, metadata file, content, and content hash."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    prefix: str
    stored_name: str
    metadata_json: bytes
    content: SpooledContent
    content_hash: str


class FileWriter:
    def __init__(self, objects: DestinationObjects) -> None:
        self._objects = objects

    def write(self, request: WriteFileRequest) -> WriteOutcome:
        file_key = f"{request.prefix}{request.stored_name}"
        metadata_key = f"{file_key}{METADATA_SUFFIX}"
        stored_keys = {listed.key for listed in self._objects.list_keys(request.prefix)}
        if file_key in stored_keys and self._objects.read_content_hash(file_key) == request.content_hash:
            return WriteOutcome.UNCHANGED
        self._objects.put(PutObjectRequest(key=metadata_key, body=io.BytesIO(request.metadata_json)))
        self._objects.put(PutObjectRequest(key=file_key, body=request.content.file, content_hash=request.content_hash))
        failure = self._objects.delete(sorted(stored_keys - {file_key, metadata_key}))
        if failure.count > 0:
            raise FileDeleteError(failure)
        return WriteOutcome.WRITTEN
