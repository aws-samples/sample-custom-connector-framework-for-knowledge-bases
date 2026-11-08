"""The toolkit's one S3 DAO: a destination bucket through the source-access session's client."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, BinaryIO

from pydantic import BaseModel, ConfigDict, SkipValidation

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client

CONTENT_HASH_METADATA = "ccf-content-hash"
"""The S3 user metadata key that holds a stored file's content hash."""
_DELETE_OBJECTS_MAX_KEYS = 1000
"""The most keys one `DeleteObjects` request takes."""


class ListedKey(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    last_modified: datetime


class PutObjectRequest(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    key: str
    body: SkipValidation[BinaryIO]
    content_hash: str | None = None
    """Stored as the object's `ccf-content-hash` metadata."""


class DeleteFailure(BaseModel):
    """The keys `DeleteObjects` didn't delete: how many, and S3's error codes, never the keys."""

    model_config = ConfigDict(frozen=True)

    count: int = 0
    codes: tuple[str, ...] = ()

    @classmethod
    def of(cls, response: Any) -> DeleteFailure:
        """Read the response's `Errors`; S3 reports per-key failures there with HTTP 200."""
        errors = response.get("Errors", [])
        return cls(count=len(errors), codes=tuple(sorted({error.get("Code", "Unknown") for error in errors})))

    def plus(self, other: DeleteFailure) -> DeleteFailure:
        return DeleteFailure(count=self.count + other.count, codes=tuple(sorted({*self.codes, *other.codes})))


class FileDeleteError(RuntimeError):
    """S3 refused to delete some keys, so they stay searchable."""

    def __init__(self, failure: DeleteFailure) -> None:
        super().__init__(f"S3 didn't delete {failure.count} key(s): {', '.join(failure.codes)}")
        self.failure = failure


class DestinationObjects:
    """Lists, reads the content hash of, puts, and deletes keys in one destination bucket."""

    def __init__(self, s3_client: S3Client, bucket_name: str) -> None:
        self._s3 = s3_client
        self._bucket_name = bucket_name

    def list_keys(self, prefix: str) -> list[ListedKey]:
        pages = self._s3.get_paginator("list_objects_v2").paginate(Bucket=self._bucket_name, Prefix=prefix)
        return [
            ListedKey(key=entry["Key"], last_modified=entry["LastModified"])
            for page in pages
            for entry in page.get("Contents", [])
        ]

    def read_content_hash(self, key: str) -> str | None:
        """The stored hash; call it only on a listed key, because S3 answers 403 for a key the role can't list."""
        return self._s3.head_object(Bucket=self._bucket_name, Key=key).get("Metadata", {}).get(CONTENT_HASH_METADATA)

    def put(self, request: PutObjectRequest) -> None:
        metadata = {} if request.content_hash is None else {CONTENT_HASH_METADATA: request.content_hash}
        self._s3.put_object(Bucket=self._bucket_name, Key=request.key, Body=request.body, Metadata=metadata)

    def delete(self, keys: list[str]) -> DeleteFailure:
        """Delete in batches of 1,000; the failure sums every batch's refused keys."""
        failure = DeleteFailure()
        for start in range(0, len(keys), _DELETE_OBJECTS_MAX_KEYS):
            batch = keys[start : start + _DELETE_OBJECTS_MAX_KEYS]
            response = self._s3.delete_objects(
                Bucket=self._bucket_name, Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True}
            )
            failure = failure.plus(DeleteFailure.of(response))
        return failure
