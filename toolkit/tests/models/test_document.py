"""Tests for in-memory documents, content detection, and checksums."""

import pytest

from custom_connector_framework.models.document import Document, DocumentMetadata
from custom_connector_framework.models.exceptions import UnsupportedDocumentError
from custom_connector_framework.types import ContentType, DocumentTypeMapper
from tests.conftest import make_document


def test_from_bytes_keeps_content_in_memory() -> None:
    document = Document.from_bytes(
        document_id="d",
        content=b"# hi",
        filename="doc.md",
        metadata=DocumentMetadata(title="T"),
    )

    assert document.content == b"# hi"
    assert document.suffix == ".md"
    assert document.infer_content_type() == ContentType.MD


def test_mapper_rejects_unsupported_extension() -> None:
    with pytest.raises(UnsupportedDocumentError):
        DocumentTypeMapper.get_content_type_from_extension("xyz")


def test_mapper_resolves_mime_type() -> None:
    assert DocumentTypeMapper.get_content_type_from_mime("application/pdf") == ContentType.PDF


def test_checksum_is_stable_for_identical_input() -> None:
    assert make_document("d").get_checksum() == make_document("d").get_checksum()


def test_checksum_changes_when_filename_changes() -> None:
    assert make_document("d", filename="doc.txt").get_checksum() != make_document("d", filename="doc.md").get_checksum()


@pytest.mark.parametrize("metadata_field", ["created_at", "last_updated_at"])
def test_checksum_changes_when_supplied_provider_timestamp_changes(metadata_field: str) -> None:
    first_checksum = make_document("d", **{metadata_field: "2020-01-01"}).get_checksum()
    second_checksum = make_document("d", **{metadata_field: "2020-01-02"}).get_checksum()

    assert first_checksum != second_checksum


def test_checksum_changes_when_body_changes() -> None:
    assert make_document("d", b"body one").get_checksum() != make_document("d", b"body two").get_checksum()


def test_content_type_failure_does_not_expose_filename() -> None:
    document = make_document("d", filename="private-repository-name.unknown")

    with pytest.raises(UnsupportedDocumentError) as caught:
        document.infer_content_type()

    assert "private-repository-name" not in str(caught.value)
