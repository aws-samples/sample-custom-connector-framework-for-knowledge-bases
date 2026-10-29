"""The object-store contract the document sink writes through.

The sink writes through this protocol rather than a single cloud SDK, so the
write destination can be any object store (AWS S3, Google Cloud Storage, Azure
Blob Storage, MinIO, or a local filesystem) by supplying an implementation.
"""

from typing import Annotated, Protocol, runtime_checkable

from pydantic import AfterValidator, BaseModel, ConfigDict


def _validate_object_key(key: str) -> str:
    if key.startswith("/") or ".." in key.split("/"):
        raise ValueError("object key must be relative and must not contain parent-traversal segments")
    return key


ObjectKey = Annotated[str, AfterValidator(_validate_object_key)]


class PutObjectRequest(BaseModel):
    """Request to write one object to the store."""

    model_config = ConfigDict(frozen=True)

    key: ObjectKey
    body: bytes


class DeleteObjectRequest(BaseModel):
    """Request to delete one object from the store."""

    model_config = ConfigDict(frozen=True)

    key: ObjectKey


@runtime_checkable
class ObjectStore(Protocol):
    """A destination the sink can write objects to and delete objects from.

    An implementation authenticates to its target store and translates
    ``request.key`` into a location there. Keys are forward-slash-delimited
    paths such as ``connectors/cc-1/doc-1.txt``; a store that keys on something
    else translates as needed.
    """

    def put_object(self, request: PutObjectRequest) -> None:
        """Write ``request.body`` to ``request.key``, overwriting any existing object."""
        ...

    def delete_object(self, request: DeleteObjectRequest) -> None:
        """Delete the object at ``request.key``. Deleting a missing key is a no-op."""
        ...
