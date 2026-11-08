"""Streams a document's content through SHA-256 into a spooled file, checking that text stays UTF-8."""

from __future__ import annotations

import codecs
import hashlib
from tempfile import SpooledTemporaryFile
from typing import IO, Any, BinaryIO

from pydantic import BaseModel, ConfigDict, SkipValidation

_CHUNK_BYTES = 1024 * 1024
_SPOOL_BYTES = 8 * 1024 * 1024
_WHITESPACE = b" \t\r\n\x0b\x0c"


class UnreadableTextError(ValueError):
    """Content that started as UTF-8 text isn't UTF-8 further on."""


class ReadContentRequest(BaseModel):
    """A document's first bytes, the rest of its stream, and whether it is stored as text."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    head: bytes
    rest: SkipValidation[BinaryIO]
    is_text: bool


class SpooledContent(BaseModel):
    """A document's bytes, held in memory up to 8 MiB and on disk after that, and their SHA-256 so far."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    file: SkipValidation[IO[bytes]]
    digest: Any
    """The `hashlib` SHA-256 of the bytes, which the content hash copies and extends."""
    blank: bool
    """Whether the content is empty or only whitespace."""

    def content_hash(self, metadata_json: bytes) -> str:
        """Return the hash of the bytes and the canonical metadata file, as `sha256:<hex>`."""
        digest = self.digest.copy()
        digest.update(metadata_json)
        return f"sha256:{digest.hexdigest()}"

    def __enter__(self) -> SpooledContent:
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Close the spooled file, removing its disk copy when it grew past 8 MiB."""
        self.file.close()


class _Decode(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    decoder: codecs.IncrementalDecoder
    chunk: bytes
    final: bool = False


class ContentReader:
    def read(self, request: ReadContentRequest) -> SpooledContent:
        spool: IO[bytes] = SpooledTemporaryFile(max_size=_SPOOL_BYTES)  # noqa: SIM115
        digest = hashlib.sha256()
        decoder = codecs.getincrementaldecoder("utf-8")()
        blank = True
        chunk = request.head
        while chunk:
            digest.update(chunk)
            spool.write(chunk)
            blank = blank and not chunk.strip(_WHITESPACE)
            if request.is_text:
                self._decode(_Decode(decoder=decoder, chunk=chunk))
            chunk = request.rest.read(_CHUNK_BYTES)
        if request.is_text:
            self._decode(_Decode(decoder=decoder, chunk=b"", final=True))
        spool.seek(0)
        return SpooledContent(file=spool, digest=digest, blank=blank)

    @staticmethod
    def _decode(step: _Decode) -> None:
        try:
            step.decoder.decode(step.chunk, final=step.final)
        except UnicodeDecodeError as error:
            raise UnreadableTextError("The document's text isn't UTF-8") from error
