"""How each document is stored: as given, by signature, or as UTF-8 text."""

from __future__ import annotations

import pytest

from custom_connector_framework.store.destination_formats import BedrockFormat, S3Format
from custom_connector_framework.store.media_types import MediaTypeRequest, MediaTypes

PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n"
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
BEDROCK = MediaTypeRequest(file_name="README.md", head=b"", media_type=None, supported=BedrockFormat.SUPPORTED_TYPES)


def test_a_pdf_signature_wins_over_a_txt_name() -> None:
    stored = MediaTypes().resolve(BEDROCK.model_copy(update={"file_name": "report.txt", "head": PDF}))

    assert stored is not None
    assert (stored.stored_name, stored.is_text) == ("report.txt.pdf", False)


def test_source_code_is_stored_as_text_with_txt_added() -> None:
    stored = MediaTypes().resolve(BEDROCK.model_copy(update={"file_name": "app.py", "head": b"print('hello')\n"}))

    assert stored is not None
    assert (stored.stored_name, stored.is_text) == ("app.py.txt", True)


def test_markdown_keeps_its_name() -> None:
    stored = MediaTypes().resolve(BEDROCK.model_copy(update={"head": b"# Title\n"}))

    assert stored is not None and stored.stored_name == "README.md"


def test_an_unsupported_binary_is_not_stored_in_bedrock() -> None:
    assert MediaTypes().resolve(BEDROCK.model_copy(update={"file_name": "logo.png", "head": PNG})) is None


def test_bytes_that_arent_utf8_text_are_not_stored() -> None:
    request = BEDROCK.model_copy(update={"file_name": "data.bin", "head": b"\xff\xfe\x00\x01\x02"})

    assert MediaTypes().resolve(request) is None


def test_a_given_media_type_the_destination_doesnt_support_is_not_stored() -> None:
    request = BEDROCK.model_copy(
        update={"file_name": "deck.pptx", "head": b"anything", "media_type": "application/vnd.ms-powerpoint"}
    )

    assert MediaTypes().resolve(request) is None


def test_a_given_media_type_is_used_as_given() -> None:
    request = BEDROCK.model_copy(update={"file_name": "export", "head": b"a,b\n1,2\n", "media_type": "text/csv"})

    stored = MediaTypes().resolve(request)

    assert stored is not None and stored.stored_name == "export.csv"


@pytest.mark.parametrize("head", [PNG, b"\xff\xfe\x00\x01"])
def test_an_s3_destination_stores_any_bytes_under_their_name(head: bytes) -> None:
    request = BEDROCK.model_copy(update={"file_name": "logo.png", "head": head, "supported": S3Format.SUPPORTED_TYPES})

    stored = MediaTypes().resolve(request)

    assert stored is not None
    assert (stored.stored_name, stored.is_text) == ("logo.png", False)
