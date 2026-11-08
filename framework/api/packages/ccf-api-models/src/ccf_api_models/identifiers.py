"""Tenants, the references and ARNs of their resources, the IDs CCF derives, and the S3 key layout.

A destination's ARN is `<tenant ARN prefix>:destination/<destination_id>`, and each child resource appends
`/<type>/<id>` to its parent's ARN, so a document ARN names its tenant, destination, custom connector, and
collection. A document's files sit under `connector_id=<id>/collection_id=<id>/document_id=<id>/`, the
ARN's last three segments.
"""

from __future__ import annotations

import hashlib
import re
from enum import StrEnum
from typing import Annotated, Final, Protocol

from pydantic import StringConstraints

from ccf_api_models.base import ValueModel
from ccf_api_models.models import CollectionId, ConnectorId, DestinationId, DocumentId, JobId


class Partition(StrEnum):
    AWS = "aws"
    AWS_US_GOV = "aws-us-gov"
    AWS_CN = "aws-cn"
    AWS_EUSC = "aws-eusc"


PARTITION_PATTERN: Final = "|".join(partition.value for partition in Partition)
REGION_PATTERN: Final = r"(?:[a-z]{2}|eusc)(?:-[a-z]+)+-[0-9]+"
ACCOUNT_ID_PATTERN: Final = r"[0-9]{12}"
type Region = Annotated[str, StringConstraints(pattern=rf"^{REGION_PATTERN}$")]
type AccountId = Annotated[str, StringConstraints(pattern=rf"^{ACCOUNT_ID_PATTERN}$")]
DEFAULT_COLLECTION_ID: Final = "col-default"
METADATA_SUFFIX: Final = ".metadata.json"
_SOURCE_HASH_CHARACTERS = 16
_SCHEDULE_HASH_CHARACTERS = 32
_ARN_PREFIX = re.compile(rf"^arn:({PARTITION_PATTERN}):ccf:({REGION_PATTERN}):({ACCOUNT_ID_PATTERN})$")
_DOCUMENT_ARN = re.compile(
    rf"^(?P<prefix>arn:(?:{PARTITION_PATTERN}):ccf:{REGION_PATTERN}:{ACCOUNT_ID_PATTERN}):destination/(?P<destination>[^/]+)"
    r"/custom-connector/(?P<connector>[^/]+)/collection/(?P<collection>[^/]+)/document/(?P<document>[^/]+)$"
)
_OBJECT_KEY = re.compile(
    r"^connector_id=(?P<connector>[^/]+)/collection_id=(?P<collection>[^/]+)/document_id=(?P<document>[^/]+)/"
)


class DestinationScoped(Protocol):
    @property
    def destination_id(self) -> str: ...


class ConnectorScoped(DestinationScoped, Protocol):
    @property
    def connector_id(self) -> str: ...


class JobScoped(ConnectorScoped, Protocol):
    @property
    def job_id(self) -> str: ...


class TenantRequest[PayloadT](Protocol):
    """A request from one tenant, such as the backend's `OperationRequest`."""

    @property
    def tenant(self) -> TenantContext: ...

    @property
    def payload(self) -> PayloadT: ...


class TenantContext(ValueModel):
    """The account, Region, and partition of the tenant a request acts for."""

    partition: Partition
    region: Region
    account_id: AccountId

    @property
    def arn_prefix(self) -> str:
        """`arn:<partition>:ccf:<region>:<account>`, the partition key of the destinations, connectors, and jobs."""
        return f"arn:{self.partition}:ccf:{self.region}:{self.account_id}"

    @classmethod
    def from_arn_prefix(cls, arn_prefix: str) -> TenantContext:
        """Parse an exact tenant ARN prefix; anything else raises `ValueError`."""
        match = _ARN_PREFIX.fullmatch(arn_prefix)
        if match is None:
            raise ValueError("The tenant ARN prefix is invalid")
        partition, region, account_id = match.groups()
        return cls(partition=Partition(partition), region=region, account_id=account_id)


class DestinationRef(ValueModel):
    tenant: TenantContext
    destination_id: DestinationId

    @property
    def arn(self) -> str:
        return f"{self.tenant.arn_prefix}:destination/{self.destination_id}"

    @classmethod
    def of(cls, request: TenantRequest[DestinationScoped]) -> DestinationRef:
        return cls(tenant=request.tenant, destination_id=request.payload.destination_id)


class ConnectorRef(ValueModel):
    destination: DestinationRef
    connector_id: ConnectorId

    @property
    def tenant(self) -> TenantContext:
        return self.destination.tenant

    @property
    def destination_id(self) -> str:
        return self.destination.destination_id

    @property
    def arn(self) -> str:
        """The partition key of the collections and documents tables."""
        return f"{self.destination.arn}/custom-connector/{self.connector_id}"

    @property
    def destination_connector_key(self) -> str:
        """`<destination_id>#<connector_id>`, the custom connectors table's sort key."""
        return f"{self.destination_id}#{self.connector_id}"

    @property
    def key_prefix(self) -> str:
        """The S3 prefix of every file of the custom connector."""
        return f"connector_id={self.connector_id}/"

    @property
    def schedule_name(self) -> str:
        """The custom connector's EventBridge Scheduler schedule: `ccf-` plus 32 hex of its ARN's SHA-256."""
        return f"ccf-{hashlib.sha256(self.arn.encode('utf-8')).hexdigest()[:_SCHEDULE_HASH_CHARACTERS]}"

    @classmethod
    def of(cls, request: TenantRequest[ConnectorScoped]) -> ConnectorRef:
        return cls(destination=DestinationRef.of(request), connector_id=request.payload.connector_id)


class JobRef(ValueModel):
    connector: ConnectorRef
    job_id: JobId

    @property
    def arn(self) -> str:
        return f"{self.connector.arn}/job/{self.job_id}"

    @classmethod
    def of(cls, request: TenantRequest[JobScoped]) -> JobRef:
        return cls(connector=ConnectorRef.of(request), job_id=request.payload.job_id)


class CollectionRef(ValueModel):
    connector: ConnectorRef
    collection_id: CollectionId

    @property
    def arn(self) -> str:
        return f"{self.connector.arn}/collection/{self.collection_id}"


class DocumentKey(ValueModel):
    """Where one document's file and metadata file sit in the destination bucket."""

    connector_id: ConnectorId
    collection_id: CollectionId
    document_id: DocumentId

    @property
    def prefix(self) -> str:
        return f"connector_id={self.connector_id}/collection_id={self.collection_id}/document_id={self.document_id}/"

    @classmethod
    def of_object_key(cls, key: str) -> DocumentKey | None:
        """Return the key of the document an object key belongs to, or None for a key outside the layout."""
        match = _OBJECT_KEY.match(key)
        if match is None:
            return None
        return cls(connector_id=match["connector"], collection_id=match["collection"], document_id=match["document"])


class DocumentRef(ValueModel):
    collection: CollectionRef
    document_id: DocumentId

    @property
    def arn(self) -> str:
        return f"{self.collection.arn}/document/{self.document_id}"

    @property
    def key(self) -> DocumentKey:
        return DocumentKey(
            connector_id=self.collection.connector.connector_id,
            collection_id=self.collection.collection_id,
            document_id=self.document_id,
        )

    @classmethod
    def from_arn(cls, arn: str) -> DocumentRef | None:
        """Parse a CCF document ARN; None when the ARN isn't one."""
        match = _DOCUMENT_ARN.fullmatch(arn)
        if match is None:
            return None
        destination = DestinationRef(
            tenant=TenantContext.from_arn_prefix(match["prefix"]), destination_id=match["destination"]
        )
        connector = ConnectorRef(destination=destination, connector_id=match["connector"])
        collection = CollectionRef(connector=connector, collection_id=match["collection"])
        return cls(collection=collection, document_id=match["document"])


class GeneratedIdKind(StrEnum):
    """The prefixes of the random IDs CCF gives the resources it creates."""

    DESTINATION = "dst-"
    CUSTOM_CONNECTOR = "cc-"
    JOB = "ccj-"


class SourceIdKind(StrEnum):
    """The prefixes of the IDs CCF derives from a connector implementation's source IDs."""

    COLLECTION = "col-"
    DOCUMENT = "doc-"

    def of(self, source_id: str) -> str:
        """The prefix plus the first 16 hex characters of the source ID's SHA-256."""
        return f"{self.value}{hashlib.sha256(source_id.encode('utf-8')).hexdigest()[:_SOURCE_HASH_CHARACTERS]}"


class CollectionIdentity(ValueModel):
    collection_id: CollectionId
    source_collection_id: str | None
    """None for the default collection."""

    @classmethod
    def of(cls, source_collection_id: str | None) -> CollectionIdentity:
        """The collection of a source collection ID; None is the default collection."""
        if source_collection_id is None:
            return cls(collection_id=DEFAULT_COLLECTION_ID, source_collection_id=None)
        return cls(
            collection_id=SourceIdKind.COLLECTION.of(source_collection_id), source_collection_id=source_collection_id
        )
