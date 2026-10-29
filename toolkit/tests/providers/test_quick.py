"""Tests for the Amazon Quick knowledge base S3 sidecar adapter."""

import json

from custom_connector_framework.models.acl import AccessControl, AccessControlEntry, AccessEffect, PrincipalType
from custom_connector_framework.providers.quick import QuickKnowledgeBaseAdapter
from tests.conftest import make_document


def test_render_document_emits_one_quick_sidecar_with_acl() -> None:
    access_control = AccessControl(
        entries=(
            AccessControlEntry(
                principal_id="a@example.com",
                principal_type=PrincipalType.USER,
                access=AccessEffect.ALLOW,
            ),
            AccessControlEntry(
                principal_id="eng",
                principal_type=PrincipalType.GROUP,
                access=AccessEffect.DENY,
            ),
        )
    )
    document = make_document(
        "doc-1",
        title="My Title",
        source_uri="https://example.com/doc",
        attributes={"author": "CCF"},
        access_control=access_control,
    )

    rendered = QuickKnowledgeBaseAdapter().render_document(document, "prefix/doc-1.txt")

    assert rendered.sidecar is not None
    assert rendered.sidecar.key == "prefix/doc-1.txt.metadata.json"
    body = json.loads(rendered.sidecar.content)
    assert body["DocumentId"] == "doc-1"
    assert body["ContentType"] == "PLAIN_TEXT"
    assert body["Attributes"]["author"] == "CCF"
    assert body["AccessControlList"] == [
        {"Name": "a@example.com", "Type": "USER", "Access": "ALLOW"},
        {"Name": "eng", "Type": "GROUP", "Access": "DENY"},
    ]
