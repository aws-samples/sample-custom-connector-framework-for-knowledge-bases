"""Reading a streamed repository tarball into kept files."""

import io
import logging

import pytest
from pathspec import GitIgnoreSpec

from common.reading.archive_reader import ArchiveFile, ArchiveReader, ReadArchiveRequest
from tests.unit.fake_github import FakeRepository


def _read(repository: FakeRepository) -> dict[str, bytes]:
    reader = ArchiveReader(GitIgnoreSpec.from_lines(["node_modules/", "*.pyc"]))
    files = reader.read(ReadArchiveRequest(archive=io.BytesIO(repository.tarball()), max_file_bytes=1024))
    return {file.path: file.content for file in files}


def test_kept_files_are_read_with_the_top_level_folder_dropped() -> None:
    repository = FakeRepository("aws-samples/demo", {"README.md": b"# Demo\n", "src/app.py": b"print(1)\n"})

    assert _read(repository) == {"README.md": b"# Demo\n", "src/app.py": b"print(1)\n"}


def test_excluded_paths_and_oversized_files_are_skipped() -> None:
    repository = FakeRepository(
        "aws-samples/demo",
        {"node_modules/x/index.js": b"x", "app.pyc": b"x", "big.txt": b"x" * 2048, "keep.md": b"keep"},
    )

    assert _read(repository) == {"keep.md": b"keep"}


def test_symlinks_are_never_followed_or_yielded() -> None:
    repository = FakeRepository("aws-samples/demo", {"README.md": b"x"}, symlinks={"secret": "/etc/passwd"})

    assert _read(repository) == {"README.md": b"x"}


def test_a_path_with_parent_segments_is_only_a_string() -> None:
    repository = FakeRepository("aws-samples/demo", {"../outside.txt": b"x"})

    assert _read(repository) == {"../outside.txt": b"x"}


def test_each_file_carries_the_blob_sha_git_hash_object_prints() -> None:
    # `printf 'hello\\n' | git hash-object --stdin`
    assert ArchiveFile(path="hello.txt", content=b"hello\n").blob_sha == "ce013625030ba8dba906f756967f9e9ca394464a"


def test_skipped_files_are_logged_once_with_the_count_per_reason(caplog: pytest.LogCaptureFixture) -> None:
    reader = ArchiveReader(GitIgnoreSpec.from_lines(["*.pyc"]))
    repository = FakeRepository("aws-samples/demo", {"a.pyc": b"x", "b.pyc": b"x", "big.txt": b"x" * 2048})

    with caplog.at_level(logging.INFO):
        list(reader.read(ReadArchiveRequest(archive=io.BytesIO(repository.tarball()), max_file_bytes=1024)))

    [skipped] = [record for record in caplog.records if record.getMessage() == "files_skipped"]
    assert skipped.levelname == "INFO"
    assert (skipped.__dict__["excluded_path"], skipped.__dict__["too_large"]) == (2, 1)
