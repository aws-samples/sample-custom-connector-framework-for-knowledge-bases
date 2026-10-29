"""Tests for the plain S3 adapter."""

from custom_connector_framework.models.acl import AccessControl, AccessControlEntry, AccessEffect, PrincipalType
from custom_connector_framework.providers.base import KnowledgeBaseProviderAdapter
from custom_connector_framework.providers.bedrock import BedrockKnowledgeBaseAdapter
from custom_connector_framework.providers.quick import QuickKnowledgeBaseAdapter
from custom_connector_framework.providers.s3 import S3Adapter
from tests.conftest import make_document


def test_render_document_writes_no_sidecar_even_with_acl() -> None:
    access_control = AccessControl(
        entries=(
            AccessControlEntry(
                principal_id="a@example.com",
                principal_type=PrincipalType.USER,
                access=AccessEffect.ALLOW,
            ),
        )
    )

    rendered = S3Adapter().render_document(make_document("doc-1", access_control=access_control), "prefix/doc-1.txt")

    assert rendered.sidecar is None


def test_provider_adapter_deletion_api_is_absent() -> None:
    assert "render_deletion" not in KnowledgeBaseProviderAdapter.__dict__
    for adapter in (S3Adapter(), BedrockKnowledgeBaseAdapter(), QuickKnowledgeBaseAdapter()):
        assert not hasattr(adapter, "render_deletion")
