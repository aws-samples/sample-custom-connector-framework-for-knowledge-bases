"""Tests for the Bedrock managed knowledge base S3 sidecar adapter."""

import json

import pytest

from custom_connector_framework.models.acl import AccessControl, AccessControlEntry, AccessEffect, PrincipalType
from custom_connector_framework.models.exceptions import UnsupportedAclError
from custom_connector_framework.providers.bedrock import AclUnsupportedGroupPolicy, BedrockKnowledgeBaseAdapter
from tests.conftest import make_document


def _user_acl() -> AccessControl:
    return AccessControl(
        entries=(
            AccessControlEntry(
                principal_id="a@example.com",
                principal_type=PrincipalType.USER,
                access=AccessEffect.ALLOW,
            ),
        )
    )


def test_render_document_emits_one_bedrock_sidecar() -> None:
    document = make_document(
        "doc-1",
        title="My Title",
        source_uri="https://example.com/doc",
        attributes={"author": "CCF"},
        access_control=_user_acl(),
    )

    rendered = BedrockKnowledgeBaseAdapter().render_document(document, "prefix/doc-1.txt")

    assert rendered.sidecar is not None
    assert rendered.sidecar.key == "prefix/doc-1.txt.metadata.json"
    body = json.loads(rendered.sidecar.content)
    assert body["metadataAttributes"]["title"]["value"]["stringValue"] == "My Title"
    assert body["accessControlList"] == [{"Name": "a@example.com", "Type": "USER", "Access": "ALLOW"}]


def test_render_document_without_acl_omits_access_control_list() -> None:
    sidecar = BedrockKnowledgeBaseAdapter().render_document(make_document("doc-1"), "k.txt").sidecar

    assert sidecar is not None
    assert "accessControlList" not in json.loads(sidecar.content)


def test_render_document_group_error_omits_document_and_principal_ids() -> None:
    sensitive_document_id = "secret-document-customer-123"
    sensitive_group_principal_id = "secret-group-principal-456"
    group_acl = AccessControl(
        entries=(
            AccessControlEntry(
                principal_id=sensitive_group_principal_id,
                principal_type=PrincipalType.GROUP,
                access=AccessEffect.ALLOW,
            ),
        )
    )

    with pytest.raises(UnsupportedAclError) as raised:
        BedrockKnowledgeBaseAdapter().render_document(
            make_document(sensitive_document_id, access_control=group_acl),
            "k.txt",
        )

    error_message = str(raised.value)
    assert "GROUP principals" in error_message
    assert sensitive_document_id not in error_message
    assert sensitive_group_principal_id not in error_message


def test_render_document_drops_group_principal_when_configured() -> None:
    group_acl = AccessControl(
        entries=(
            AccessControlEntry(
                principal_id="eng",
                principal_type=PrincipalType.GROUP,
                access=AccessEffect.ALLOW,
            ),
        )
    )
    adapter = BedrockKnowledgeBaseAdapter(unsupported_group_policy=AclUnsupportedGroupPolicy.DROP)
    sidecar = adapter.render_document(make_document("doc-1", access_control=group_acl), "k.txt").sidecar

    assert sidecar is not None
    assert "accessControlList" not in json.loads(sidecar.content)
