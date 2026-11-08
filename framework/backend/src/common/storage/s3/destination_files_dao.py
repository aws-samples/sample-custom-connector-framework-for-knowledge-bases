"""Deletes documents' files from a destination bucket through the tenant's `CcfDestinationRole`.

The framework holds no S3 permission on tenant buckets: each call assumes the destination's role. For each
document, every key under its labeled prefix is listed and deleted, so a file and its metadata file go
together whatever the file's name.
"""

from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError
from ccf_api_models.base import ValueModel
from ccf_api_models.identifiers import CollectionRef, DocumentRef
from pydantic import ConfigDict

from common.errors import DependencyUnavailableError, FailedAwsCall
from common.tenant_role_clients import AssumeTenantRoleRequest, TenantRoleClients

_DELETE_OBJECTS_MAX_KEYS = 1000
"""The most keys one `DeleteObjects` request takes."""


class DeleteDocumentFilesRequest(ValueModel):
    """The documents of one collection whose files to delete, and the destination that holds them."""

    collection: CollectionRef
    role_arn: str
    bucket_name: str
    document_ids: tuple[str, ...]


class _Listing(ValueModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    s3: Any
    bucket_name: str
    prefix: str


class _Deletion(ValueModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    s3: Any
    bucket_name: str
    keys: tuple[str, ...]


class DestinationFilesDao:
    """Lists and deletes the files under documents' prefixes; a document with no files is already deleted."""

    def __init__(self, clients: TenantRoleClients, external_id: str) -> None:
        self._clients = clients
        self._external_id = external_id

    def delete_document_files(self, request: DeleteDocumentFilesRequest) -> None:
        session_name = f"ccf-delete-{request.collection.connector.connector_id}"
        s3 = self._clients.assume(
            AssumeTenantRoleRequest(role_arn=request.role_arn, external_id=self._external_id, session_name=session_name)
        ).client("s3")
        keys = [
            key
            for document_id in request.document_ids
            for key in self._keys(
                _Listing(
                    s3=s3,
                    bucket_name=request.bucket_name,
                    prefix=DocumentRef(collection=request.collection, document_id=document_id).key.prefix,
                )
            )
        ]
        for start in range(0, len(keys), _DELETE_OBJECTS_MAX_KEYS):
            batch = tuple(keys[start : start + _DELETE_OBJECTS_MAX_KEYS])
            self._delete(_Deletion(s3=s3, bucket_name=request.bucket_name, keys=batch))

    @staticmethod
    def _keys(listing: _Listing) -> list[str]:
        try:
            pages = listing.s3.get_paginator("list_objects_v2").paginate(
                Bucket=listing.bucket_name, Prefix=listing.prefix
            )
            return [entry["Key"] for page in pages for entry in page.get("Contents", [])]
        except ClientError as error:
            raise FailedAwsCall(error=error, action="Document file listing").to_error() from error

    @staticmethod
    def _delete(deletion: _Deletion) -> None:
        try:
            response = deletion.s3.delete_objects(
                Bucket=deletion.bucket_name, Delete={"Objects": [{"Key": key} for key in deletion.keys], "Quiet": True}
            )
        except ClientError as error:
            raise FailedAwsCall(error=error, action="Document file deletion").to_error() from error
        if response.get("Errors"):
            raise DependencyUnavailableError("Document file deletion failed for some keys")
