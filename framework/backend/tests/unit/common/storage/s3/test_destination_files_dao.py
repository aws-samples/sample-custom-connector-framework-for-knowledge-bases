"""Deleting documents' files through the destination role, one labeled prefix per document."""

from __future__ import annotations

from typing import Any

import boto3
import pytest
from ccf_api_models.identifiers import CollectionRef, ConnectorRef, DestinationRef, Partition, TenantContext

from common.errors import DependencyUnavailableError
from common.storage.s3.destination_files_dao import DeleteDocumentFilesRequest, DestinationFilesDao
from common.tenant_role_clients import AssumeTenantRoleRequest

WORKFLOW_ARN = "arn:aws:states:us-east-1:444455556666:stateMachine:CcfDestinationWorkflow"
ROLE_ARN = "arn:aws:iam::111122223333:role/ccf/destinations/CcfDestinationRole-docs"
BUCKET = "ccf-dst-7c41e9a2b5d0-111122223333-us-east-1"
TENANT = TenantContext(partition=Partition.AWS, region="us-east-1", account_id="111122223333")
CONNECTOR = ConnectorRef(
    destination=DestinationRef(tenant=TENANT, destination_id="dst-7c41e9a2b5d0"), connector_id="cc-e020d65d76c0"
)
COLLECTION = CollectionRef(connector=CONNECTOR, collection_id="col-3f9a1c2b5d7e9f01")
GONE, KEPT = "doc-8d41e07a9c3f2b6e", "doc-0123456789abcdef"


def prefix(document_id: str) -> str:
    return f"connector_id={CONNECTOR.connector_id}/collection_id={COLLECTION.collection_id}/document_id={document_id}/"


class RecordingS3:
    """A destination bucket's keys and the calls made on it."""

    def __init__(self, keys: set[str]) -> None:
        self.keys = keys
        self.calls: list[str] = []
        self.deleted_batches: list[int] = []
        self.refuse = False

    def get_paginator(self, name: str) -> RecordingS3:
        return self

    def paginate(self, Bucket: str, Prefix: str) -> list[dict[str, Any]]:  # noqa: N803
        assert Bucket == BUCKET
        self.calls.append(f"ListObjectsV2 {Prefix}")
        return [{"Contents": [{"Key": key} for key in sorted(self.keys) if key.startswith(Prefix)]}]

    def delete_objects(self, Bucket: str, Delete: dict[str, Any]) -> dict[str, Any]:  # noqa: N803
        self.calls.append("DeleteObjects")
        self.deleted_batches.append(len(Delete["Objects"]))
        if self.refuse:
            return {"Errors": [{"Key": Delete["Objects"][0]["Key"], "Code": "AccessDenied"}]}
        self.keys -= {entry["Key"] for entry in Delete["Objects"]}
        return {}


class RecordingClients:
    """Stands in for `TenantRoleClients`; records each role it was asked to assume."""

    def __init__(self, s3: RecordingS3) -> None:
        self.s3 = s3
        self.requests: list[AssumeTenantRoleRequest] = []

    def assume(self, request: AssumeTenantRoleRequest) -> RecordingClients:
        self.requests.append(request)
        return self

    def client(self, service_name: str) -> RecordingS3:
        assert service_name == "s3"
        return self.s3


def delete(clients: RecordingClients, document_ids: tuple[str, ...]) -> None:
    files = DestinationFilesDao(clients, WORKFLOW_ARN)  # type: ignore[arg-type]
    files.delete_document_files(
        DeleteDocumentFilesRequest(
            collection=COLLECTION, role_arn=ROLE_ARN, bucket_name=BUCKET, document_ids=document_ids
        )
    )


def test_each_documents_labeled_prefix_is_listed_and_deleted_through_the_destination_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lambda_calls: list[object] = []
    # Any S3 client built with the Lambda's own credentials during the delete records here.
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: lambda_calls.append(args))
    monkeypatch.setattr(boto3, "resource", lambda *args, **kwargs: lambda_calls.append(args))
    kept = f"{prefix(KEPT)}LICENSE"
    clients = RecordingClients(
        RecordingS3({f"{prefix(GONE)}README.md", f"{prefix(GONE)}README.md.metadata.json", kept})
    )

    delete(clients, (GONE,))

    assert clients.s3.calls == [f"ListObjectsV2 {prefix(GONE)}", "DeleteObjects"]
    assert clients.s3.keys == {kept}
    assert clients.requests == [
        AssumeTenantRoleRequest(
            role_arn=ROLE_ARN, external_id=WORKFLOW_ARN, session_name=f"ccf-delete-{CONNECTOR.connector_id}"
        )
    ]
    assert lambda_calls == []


def test_a_delete_objects_response_with_errors_raises() -> None:
    clients = RecordingClients(RecordingS3({f"{prefix(GONE)}README.md"}))
    clients.s3.refuse = True

    with pytest.raises(DependencyUnavailableError):
        delete(clients, (GONE,))


def test_more_than_1000_keys_are_deleted_in_batches_of_1000() -> None:
    clients = RecordingClients(RecordingS3({f"{prefix(GONE)}page-{number}.md" for number in range(1_001)}))

    delete(clients, (GONE,))

    assert clients.s3.deleted_batches == [1_000, 1]
    assert clients.s3.keys == set()
