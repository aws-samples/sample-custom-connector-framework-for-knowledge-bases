"""Shared fixtures for toolkit tests."""

import boto3
import pytest
from moto import mock_aws

from custom_connector_framework.models.acl import AccessControl
from custom_connector_framework.models.document import Document, DocumentMetadata


@pytest.fixture
def aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture
def s3_bucket(aws_credentials: None) -> str:
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        bucket_name = "ccf-test-bucket"
        client.create_bucket(Bucket=bucket_name)
        yield bucket_name


@pytest.fixture
def s3_client(aws_credentials: None, s3_bucket: str) -> boto3.client:
    return boto3.client("s3", region_name="us-east-1")


def make_document(
    document_id: str,
    content: bytes = b"hello world",
    filename: str = "doc.txt",
    title: str = "Title",
    access_control: AccessControl | None = None,
    **metadata_kwargs: object,
) -> Document:
    metadata = DocumentMetadata(
        title=title,
        access_control=access_control or AccessControl(),
        **metadata_kwargs,
    )
    return Document.from_bytes(
        document_id=document_id,
        content=content,
        filename=filename,
        metadata=metadata,
    )
