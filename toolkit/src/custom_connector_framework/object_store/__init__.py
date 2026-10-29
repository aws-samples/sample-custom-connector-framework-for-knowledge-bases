"""Pluggable object-store destinations for the document sink."""

from custom_connector_framework.object_store.base import (
    DeleteObjectRequest,
    ObjectStore,
    PutObjectRequest,
)
from custom_connector_framework.object_store.local import LocalFileObjectStore
from custom_connector_framework.object_store.s3 import S3ObjectStore

__all__ = [
    "DeleteObjectRequest",
    "LocalFileObjectStore",
    "ObjectStore",
    "PutObjectRequest",
    "S3ObjectStore",
]
