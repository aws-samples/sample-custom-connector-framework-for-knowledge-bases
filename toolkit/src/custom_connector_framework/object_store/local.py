"""An object store that writes each object as a file under a local directory."""

from pathlib import Path

from custom_connector_framework.object_store.base import (
    DeleteObjectRequest,
    ObjectStore,
    PutObjectRequest,
)


class LocalFileObjectStore(ObjectStore):
    """Writes objects to a local directory.

    Useful for tests, for local development, and for a connector whose
    destination is a filesystem mount such as an on-premises object-store
    gateway. A key is treated as a path relative to ``root``.
    """

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    def put_object(self, request: PutObjectRequest) -> None:
        """Write the object to ``<root>/<key>``, creating parent directories."""
        target = self._root / request.key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(request.body)

    def delete_object(self, request: DeleteObjectRequest) -> None:
        """Delete ``<root>/<key>`` if it exists."""
        target = self._root / request.key
        target.unlink(missing_ok=True)
