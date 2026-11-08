"""Read one repository tarball as a stream, yielding each kept file's path, bytes, and git blob SHA.

The tarball is decompressed as it arrives from the network, and each kept file is read
into memory only when it is yielded, so memory holds one file and nothing is written to
disk. Paths are only ever used as strings, never as filesystem locations. GitHub prefixes
every member with one top-level folder, `{owner}-{repo}-{sha}/`, which is dropped from
the repository path the patterns and documents use.
"""

import hashlib
import logging
import tarfile
from collections import Counter
from collections.abc import Iterator
from enum import Enum
from pathlib import PurePosixPath
from typing import BinaryIO

from pathspec import GitIgnoreSpec
from pydantic import BaseModel, ConfigDict, SkipValidation

logger = logging.getLogger(__name__)

_TOP_LEVEL_FOLDER_PARTS = 1


class FileSkipReason(str, Enum):
    """Why the reader left a file out."""

    EXCLUDED_PATH = "excluded_path"
    TOO_LARGE = "too_large"


class ReadArchiveRequest(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    archive: SkipValidation[BinaryIO]
    """The gzipped tarball, read once from start to end."""
    max_file_bytes: int


class ArchiveFile(BaseModel):
    """One kept file: its path in the repository and its bytes."""

    model_config = ConfigDict(frozen=True)

    path: str
    content: bytes

    @property
    def blob_sha(self) -> str:
        """The SHA-1 git computes for the file, which equals the blob SHA GitHub reports."""
        header = f"blob {len(self.content)}\0".encode()
        return hashlib.sha1(header + self.content, usedforsecurity=False).hexdigest()


class ArchiveReader:
    """Yields the regular files of a repository tarball that no excluded path pattern matches."""

    def __init__(self, excluded_paths: GitIgnoreSpec) -> None:
        self._excluded_paths = excluded_paths

    def read(self, request: ReadArchiveRequest) -> Iterator[ArchiveFile]:
        skipped: Counter[FileSkipReason] = Counter()
        with tarfile.open(fileobj=request.archive, mode="r|gz") as archive:
            for member in archive:
                parts = PurePosixPath(member.name.lstrip("/")).parts[_TOP_LEVEL_FOLDER_PARTS:]
                if not member.isfile() or not parts:
                    continue
                path = "/".join(parts)
                if self._excluded_paths.match_file(path):
                    skipped[FileSkipReason.EXCLUDED_PATH] += 1
                    continue
                if member.size > request.max_file_bytes:
                    skipped[FileSkipReason.TOO_LARGE] += 1
                    continue
                stream = archive.extractfile(member)
                if stream is None:
                    continue
                with stream:
                    yield ArchiveFile(path=path, content=stream.read())
        if skipped:
            logger.info("files_skipped", extra={reason.value: count for reason, count in skipped.items()})
