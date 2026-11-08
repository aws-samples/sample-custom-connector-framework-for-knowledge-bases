"""Choosing how a document is stored: as given, by a known binary signature, or as UTF-8 text.

A managed knowledge base ingests only some file types, chosen by the stored file's
extension; the destination's format supplies them as `SupportedTypes`. A given `media_type`
is used as given. Otherwise the store looks at the first 8 KB: a known binary signature
(`filetype`) first, then UTF-8 text. Text whose extension the destination doesn't support
gets `.txt` added (`App.py` is stored as `App.py.txt`), and a binary file whose name doesn't
carry its type's extension gets that extension added. A destination that supports every
type stores any bytes under their name.
"""

from __future__ import annotations

import codecs
from pathlib import PurePosixPath

import filetype  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict

DETECTION_BYTES = 8 * 1024

_EXTENSION_BY_MEDIA_TYPE = {
    "application/pdf": "pdf",
    "text/html": "html",
    "application/xml": "xml",
    "text/xml": "xml",
    "application/xslt+xml": "xslt",
    "text/markdown": "md",
    "text/csv": "csv",
    "application/vnd.ms-excel": "xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/json": "json",
    "application/rtf": "rtf",
    "text/rtf": "rtf",
    "application/vnd.ms-powerpoint": "ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "text/plain": "txt",
}
_TEXT_EXTENSIONS = frozenset({"txt", "md", "html", "csv", "json", "xml", "xslt"})


class SupportedTypes(BaseModel):
    """The file types a destination ingests: every type, or the listed extensions."""

    model_config = ConfigDict(frozen=True)

    any_type: bool
    extensions: frozenset[str] = frozenset()

    def supports(self, extension: str) -> bool:
        return self.any_type or extension in self.extensions


class MediaTypeRequest(BaseModel):
    """A document's file name, its first bytes, the media type the implementation gave, and the types to store."""

    model_config = ConfigDict(frozen=True)

    file_name: str
    head: bytes
    media_type: str | None
    supported: SupportedTypes


class StoredMedia(BaseModel):
    """How the document is stored: its stored file name, and whether it is text."""

    model_config = ConfigDict(frozen=True)

    stored_name: str
    is_text: bool


class _Typed(BaseModel):
    model_config = ConfigDict(frozen=True)

    request: MediaTypeRequest
    extension: str
    is_text: bool


class MediaTypes:
    """Resolves how a document is stored, or None when the destination can't ingest it."""

    def resolve(self, request: MediaTypeRequest) -> StoredMedia | None:
        if request.media_type is not None:
            return self._resolve_given_type(request)
        guessed = filetype.guess(request.head)
        if guessed is not None:
            return self._store_as_typed(_Typed(request=request, extension=str(guessed.extension), is_text=False))
        if not self._is_utf8_start(request.head):
            return self._store_untyped(request)
        return self._store_as_text(request)

    def _resolve_given_type(self, request: MediaTypeRequest) -> StoredMedia | None:
        extension = _EXTENSION_BY_MEDIA_TYPE.get(request.media_type or "")
        if extension is None:
            return self._store_untyped(request)
        return self._store_as_typed(_Typed(request=request, extension=extension, is_text=extension in _TEXT_EXTENSIONS))

    @staticmethod
    def _store_as_text(request: MediaTypeRequest) -> StoredMedia:
        extension = PurePosixPath(request.file_name).suffix.lower().lstrip(".")
        supported = request.supported
        if supported.any_type or (supported.supports(extension) and extension in _TEXT_EXTENSIONS):
            return StoredMedia(stored_name=request.file_name, is_text=True)
        return StoredMedia(stored_name=f"{request.file_name}.txt", is_text=True)

    @staticmethod
    def _store_as_typed(typed: _Typed) -> StoredMedia | None:
        request = typed.request
        if not request.supported.supports(typed.extension):
            return None
        name_extension = PurePosixPath(request.file_name).suffix.lower().lstrip(".")
        stored_name = (
            request.file_name if name_extension == typed.extension else f"{request.file_name}.{typed.extension}"
        )
        return StoredMedia(stored_name=stored_name, is_text=typed.is_text)

    @staticmethod
    def _store_untyped(request: MediaTypeRequest) -> StoredMedia | None:
        """Store bytes of no known type under their name, only where the destination stores every type."""
        if not request.supported.any_type:
            return None
        return StoredMedia(stored_name=request.file_name, is_text=False)

    @staticmethod
    def _is_utf8_start(head: bytes) -> bool:
        """Return whether the bytes are UTF-8, allowing a character cut off at the end of the sample."""
        try:
            codecs.getincrementaldecoder("utf-8")().decode(head, final=False)
        except UnicodeDecodeError:
            return False
        return b"\x00" not in head
