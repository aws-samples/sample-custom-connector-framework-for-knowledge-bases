"""Reading a document's content: UTF-8 checks across chunks, blank content, and the content hash."""

from __future__ import annotations

import hashlib
import io

import pytest

from custom_connector_framework.store.content_reader import ContentReader, ReadContentRequest, UnreadableTextError

CHUNK = 1024 * 1024


def read(head: bytes, rest: bytes, is_text: bool = True) -> tuple[bytes, bool, str]:
    """The spooled bytes, whether they're blank, and the content hash with an empty metadata file."""
    with ContentReader().read(ReadContentRequest(head=head, rest=io.BytesIO(rest), is_text=is_text)) as content:
        return content.file.read(), content.blank, content.content_hash(b"")


def test_utf8_text_cut_across_chunks_reads() -> None:
    euro = "€".encode()
    rest = b"a" * (CHUNK - 1) + euro

    data, _, _ = read(b"x", rest)

    assert data == b"x" + rest


def test_text_that_isnt_utf8_further_on_raises() -> None:
    with pytest.raises(UnreadableTextError):
        read(b"caf\xc3\xa9 ", b"ok \xff")


def test_binary_content_isnt_checked_as_text() -> None:
    data, _, _ = read(b"%PDF", b"\xff\xfe", is_text=False)

    assert data == b"%PDF\xff\xfe"


@pytest.mark.parametrize(("head", "rest", "blank"), [(b"", b"", True), (b"  \n", b"\t", True), (b" ", b"x", False)])
def test_empty_or_whitespace_content_is_blank(head: bytes, rest: bytes, blank: bool) -> None:
    assert read(head, rest)[1] is blank


def test_the_content_hash_covers_the_bytes_and_the_metadata_file() -> None:
    metadata = b'{"title":"Hi"}'
    with ContentReader().read(ReadContentRequest(head=b"# Hi\n", rest=io.BytesIO(b""), is_text=True)) as content:
        with_metadata = content.content_hash(metadata)
        without = content.content_hash(b"")

    assert with_metadata == "sha256:" + hashlib.sha256(b"# Hi\n" + metadata).hexdigest()
    assert without != with_metadata
