"""An in-memory destination bucket with the S3 calls the document store and the file sweep make."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

from botocore.exceptions import ClientError
from pydantic import BaseModel, ConfigDict

_LIST_PAGE_SIZE = 1000


class _StoredObject(BaseModel):
    model_config = ConfigDict(frozen=True)

    body: bytes
    metadata: dict[str, str]
    last_modified: datetime


class _ListObjectsPaginator:
    def __init__(self, s3: FakeS3) -> None:
        self._s3 = s3

    def paginate(self, **arguments: Any) -> Iterator[dict[str, Any]]:
        prefix = arguments.get("Prefix", "")
        self._s3.calls.append(("ListObjectsV2", prefix))
        keys = sorted(key for key in self._s3.objects if key.startswith(prefix))
        for start in range(0, max(len(keys), 1), _LIST_PAGE_SIZE):
            yield {
                "Contents": [
                    {
                        "Key": key,
                        "LastModified": self._s3.objects[key].last_modified,
                        "Size": len(self._s3.objects[key].body),
                    }
                    for key in keys[start : start + _LIST_PAGE_SIZE]
                ]
            }


class FakeS3:
    """One in-memory bucket with the S3 calls the store and the sweep make, each recorded with its key or prefix."""

    def __init__(self) -> None:
        self.objects: dict[str, _StoredObject] = {}
        self.calls: list[tuple[str, str]] = []
        self.refused_deletes: set[str] = set()
        """Keys `DeleteObjects` refuses with `AccessDenied` in its `Errors`, as S3 does with HTTP 200."""
        self._clock = datetime(2026, 1, 1, tzinfo=UTC)

    def put_object(self, **arguments: Any) -> dict[str, Any]:
        key = arguments["Key"]
        self.calls.append(("PutObject", key))
        self._clock += timedelta(seconds=1)
        self.objects[key] = _StoredObject(
            body=arguments["Body"].read(),
            metadata=dict(arguments.get("Metadata", {})),
            last_modified=self._clock,
        )
        return {}

    def head_object(self, **arguments: Any) -> dict[str, Any]:
        key = arguments["Key"]
        self.calls.append(("HeadObject", key))
        stored = self.objects.get(key)
        if stored is None:
            raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")
        return {"Metadata": dict(stored.metadata), "LastModified": stored.last_modified}

    def delete_objects(self, **arguments: Any) -> dict[str, Any]:
        errors: list[dict[str, str]] = []
        for entry in arguments["Delete"]["Objects"]:
            key = entry["Key"]
            self.calls.append(("DeleteObjects", key))
            if key in self.refused_deletes:
                errors.append({"Key": key, "Code": "AccessDenied", "Message": "Access Denied"})
                continue
            self.objects.pop(key, None)
        return {"Errors": errors} if errors else {}

    def get_paginator(self, operation_name: str) -> _ListObjectsPaginator:
        self.calls.append(("GetPaginator", operation_name))
        return _ListObjectsPaginator(self)

    def remove_under(self, prefix: str) -> None:
        """Remove every key under the prefix, as the framework does with its own role; not recorded as a call."""
        for key in [key for key in self.objects if key.startswith(prefix)]:
            del self.objects[key]

    def files(self) -> dict[str, bytes]:
        """Each key's body."""
        return {key: stored.body for key, stored in sorted(self.objects.items())}
