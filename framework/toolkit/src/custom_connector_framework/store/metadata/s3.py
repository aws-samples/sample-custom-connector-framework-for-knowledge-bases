"""The metadata file the store writes beside each document in an `S3` destination.

No managed knowledge base reads an `S3` destination, so the file holds the document's ARN,
title, and attributes for whatever reads the bucket, and no access control list: access to
the bucket is governed by IAM.
"""

from __future__ import annotations

from enum import Enum
from typing import ClassVar

from custom_connector_framework.store.metadata.canonical_json import CanonicalJsonFile

type S3Value = str | int | list[str]


class S3OwnAttribute(str, Enum):
    """The `attributes` the store writes itself."""

    DOCUMENT_ARN = "document_arn"
    SOURCE_URI = "source_uri"


class S3MetadataFile(CanonicalJsonFile):
    """An `S3` destination's metadata file."""

    RESERVED_NAMES: ClassVar[frozenset[str]] = frozenset(name.value for name in S3OwnAttribute)

    title: str
    attributes: dict[str, S3Value]
