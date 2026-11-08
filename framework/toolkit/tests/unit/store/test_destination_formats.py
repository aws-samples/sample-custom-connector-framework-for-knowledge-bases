"""Each destination type's metadata file, access-control rule, and the one choice of format."""

from __future__ import annotations

import io
import json
from typing import Any

import pytest
from ccf_api_models.models import Destination, DestinationResources, DestinationStatus, DestinationType

from custom_connector_framework.store.access_control import (
    AccessControl,
    AccessControlEntry,
    AccessEffect,
    PrincipalType,
    UnsupportedAccessControlError,
)
from custom_connector_framework.store.destination_formats import (
    AccessControlUse,
    BedrockFormat,
    DestinationFormats,
    MetadataFileRequest,
    QuickFormat,
    S3Format,
)
from custom_connector_framework.store.models import (
    RESERVED_ATTRIBUTE_NAMES,
    DocumentAttribute,
    NumberAttribute,
    PutDocumentRequest,
    StringAttribute,
)

DOCUMENT_ARN = "arn:aws:ccf:us-east-1:111122223333:destination/dst-7c41e9a2b5d0/document/doc-1"
USER = AccessControlEntry(principal_id="dev@example.com", principal_type=PrincipalType.USER, access=AccessEffect.ALLOW)
GROUP = AccessControlEntry(principal_id="maintainers", principal_type=PrincipalType.GROUP, access=AccessEffect.DENY)
USERS = AccessControl(entries=(USER,))
USERS_AND_GROUPS = AccessControl(entries=(USER, GROUP))


def file_request(access_control: AccessControl | None) -> MetadataFileRequest:
    """A metadata file request for a document with two attributes, a title, a source URI, and the given list."""
    document = PutDocumentRequest(
        document_id="README.md",
        file_name="README.md",
        content=io.BytesIO(b"# Hello\n"),
        attributes=(
            DocumentAttribute(name="stars", value=NumberAttribute(value=42)),
            DocumentAttribute(name="language", value=StringAttribute(value="Python")),
        ),
        title="Read me",
        source_uri="https://example.com/README.md",
        access_control=access_control,
    )
    return MetadataFileRequest(
        request=document, document_id="doc-1", document_arn=DOCUMENT_ARN, stored_name="README.md"
    )


def parsed(metadata_file: bytes) -> dict[str, Any]:
    return json.loads(metadata_file)  # type: ignore[no-any-return]


def test_a_bedrock_file_holds_the_attributes_the_arn_the_title_and_the_source_uri() -> None:
    attributes = parsed(BedrockFormat(False).metadata_file(file_request(None)))["metadataAttributes"]

    assert attributes == {
        "stars": {"value": {"type": "NUMBER", "numberValue": 42}},
        "language": {"value": {"type": "STRING", "stringValue": "Python"}},
        "document_arn": {"value": {"type": "STRING", "stringValue": DOCUMENT_ARN}},
        "title": {"value": {"type": "STRING", "stringValue": "Read me"}},
        "source_uri": {"value": {"type": "STRING", "stringValue": "https://example.com/README.md"}},
    }


def test_a_quick_file_holds_the_title_the_content_type_of_the_stored_name_and_quicks_source_uri() -> None:
    request = file_request(None).model_copy(update={"stored_name": "app.py.txt"})

    body = parsed(QuickFormat().metadata_file(request))

    assert (body["DocumentId"], body["Title"], body["ContentType"]) == ("doc-1", "Read me", "PLAIN_TEXT")
    assert body["Attributes"] == {
        "stars": 42,
        "language": "Python",
        "document_arn": DOCUMENT_ARN,
        "_source_uri": "https://example.com/README.md",
    }


def test_an_s3_file_holds_the_title_and_attributes_and_no_list() -> None:
    body = parsed(S3Format().metadata_file(file_request(USERS_AND_GROUPS)))

    assert body == {
        "title": "Read me",
        "attributes": {
            "stars": 42,
            "language": "Python",
            "document_arn": DOCUMENT_ARN,
            "source_uri": "https://example.com/README.md",
        },
    }
    assert S3Format().check_access_control(USERS_AND_GROUPS) is AccessControlUse.NONE


@pytest.mark.parametrize(("access_control_enabled", "written"), [(True, True), (False, False)])
def test_a_bedrock_file_holds_the_access_list_only_with_access_control_on(
    access_control_enabled: bool, written: bool
) -> None:
    body = parsed(BedrockFormat(access_control_enabled).metadata_file(file_request(USERS)))

    assert ("accessControlList" in body) is written
    if written:
        assert body["accessControlList"] == [{"Name": "dev@example.com", "Type": "USER", "Access": "ALLOW"}]


def test_bedrock_with_access_control_on_rejects_a_group_entry() -> None:
    with pytest.raises(UnsupportedAccessControlError):
        BedrockFormat(True).check_access_control(USERS_AND_GROUPS)


@pytest.mark.parametrize(
    ("access_control", "use"),
    [
        (USERS_AND_GROUPS, AccessControlUse.LEFT_OUT),
        (AccessControl(), AccessControlUse.NONE),
        (None, AccessControlUse.NONE),
    ],
)
def test_bedrock_with_access_control_off_reports_a_list_as_left_out(
    access_control: AccessControl | None, use: AccessControlUse
) -> None:
    assert BedrockFormat(False).check_access_control(access_control) is use


def test_a_quick_file_holds_users_and_groups() -> None:
    body = parsed(QuickFormat().metadata_file(file_request(USERS_AND_GROUPS)))

    assert body["AccessControlList"] == [
        {"Name": "dev@example.com", "Type": "USER", "Access": "ALLOW"},
        {"Name": "maintainers", "Type": "GROUP", "Access": "DENY"},
    ]
    assert QuickFormat().check_access_control(USERS_AND_GROUPS) is AccessControlUse.WRITTEN


@pytest.mark.parametrize(
    ("destination_type", "format_type"),
    [(DestinationType.BEDROCK, BedrockFormat), (DestinationType.QUICK, QuickFormat), (DestinationType.S3, S3Format)],
)
def test_select_returns_the_format_for_each_destination_type(
    destination_type: DestinationType, format_type: type[BedrockFormat | QuickFormat | S3Format]
) -> None:
    destination = Destination(
        destination_id="dst-7c41e9a2b5d0",
        arn="arn:aws:ccf:us-east-1:111122223333:destination/dst-7c41e9a2b5d0",
        name="docs",
        type=destination_type,
        access_control_enabled=False,
        status=DestinationStatus.ACTIVE,
        role_arn="arn:aws:iam::111122223333:role/ccf/destinations/CcfDestinationRole-docs",
        destination_resources=DestinationResources(bucket_arn="arn:aws:s3:::bucket", bucket_name="bucket"),
        created_at=1.0,
        updated_at=1.0,
    )

    assert isinstance(DestinationFormats().select(destination), format_type)


def test_the_reserved_attribute_names_are_every_name_a_format_writes_itself() -> None:
    assert frozenset({"document_arn", "title", "source_uri", "_source_uri"}) == RESERVED_ATTRIBUTE_NAMES
