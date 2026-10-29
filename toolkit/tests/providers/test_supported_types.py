"""Tests for the per-provider supported-extension mapper."""

from custom_connector_framework.providers.bedrock import BedrockKnowledgeBaseAdapter
from custom_connector_framework.providers.quick import QuickKnowledgeBaseAdapter
from custom_connector_framework.providers.s3 import S3Adapter


def test_bedrock_keeps_supported_extension() -> None:
    assert BedrockKnowledgeBaseAdapter().object_suffix(".pdf") == ".pdf"


def test_bedrock_appends_txt_to_unsupported_extension() -> None:
    assert BedrockKnowledgeBaseAdapter().object_suffix(".java") == ".java.txt"


def test_bedrock_does_not_support_json_so_appends_txt() -> None:
    # Bedrock's S3 data source does not ingest .json; Quick does.
    assert BedrockKnowledgeBaseAdapter().object_suffix(".json") == ".json.txt"


def test_quick_supports_json_natively() -> None:
    assert QuickKnowledgeBaseAdapter().object_suffix(".json") == ".json"


def test_quick_appends_txt_to_unsupported_extension() -> None:
    assert QuickKnowledgeBaseAdapter().object_suffix(".java") == ".java.txt"


def test_extension_match_is_case_insensitive() -> None:
    assert BedrockKnowledgeBaseAdapter().object_suffix(".PDF") == ".PDF"


def test_empty_suffix_is_left_unchanged() -> None:
    assert BedrockKnowledgeBaseAdapter().object_suffix("") == ""


def test_plain_s3_never_rewrites() -> None:
    assert S3Adapter().object_suffix(".java") == ".java"
    assert S3Adapter().object_suffix(".anything") == ".anything"
