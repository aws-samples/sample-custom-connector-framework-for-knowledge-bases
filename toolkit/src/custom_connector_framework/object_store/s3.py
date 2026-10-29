"""An object store backed by a single AWS S3 bucket."""

from botocore.client import BaseClient

from custom_connector_framework.object_store.base import (
    DeleteObjectRequest,
    ObjectStore,
    PutObjectRequest,
)


class S3ObjectStore(ObjectStore):
    """Writes objects to one S3 bucket through a boto3 S3 client."""

    def __init__(self, s3_client: BaseClient, bucket: str) -> None:
        self._s3_client = s3_client
        self._bucket = bucket

    @property
    def bucket(self) -> str:
        """The S3 bucket objects are written to."""
        return self._bucket

    def put_object(self, request: PutObjectRequest) -> None:
        """Write the object to ``s3://<bucket>/<key>``."""
        self._s3_client.put_object(Bucket=self._bucket, Key=request.key, Body=request.body)

    def delete_object(self, request: DeleteObjectRequest) -> None:
        """Delete ``s3://<bucket>/<key>`` (a missing key is a no-op in S3)."""
        self._s3_client.delete_object(Bucket=self._bucket, Key=request.key)
