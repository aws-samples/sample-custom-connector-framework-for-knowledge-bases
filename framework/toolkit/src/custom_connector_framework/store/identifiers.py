"""The custom connector a store writes for, and the reference of each document it writes."""

from __future__ import annotations

from ccf_api_models.base import ValueModel
from ccf_api_models.identifiers import CollectionRef, ConnectorRef, DestinationRef, DocumentRef, TenantContext


class CollectionDocument(ValueModel):
    """A document's framework collection and document IDs."""

    collection_id: str
    document_id: str


class StoreTarget(ValueModel):
    """The custom connector the store writes for, and its destination."""

    tenant_arn_prefix: str
    destination_id: str
    connector_id: str

    def document(self, document: CollectionDocument) -> DocumentRef:
        """The document's reference, which gives its ARN and its prefix in the destination bucket."""
        destination = DestinationRef(
            tenant=TenantContext.from_arn_prefix(self.tenant_arn_prefix), destination_id=self.destination_id
        )
        collection = CollectionRef(
            connector=ConnectorRef(destination=destination, connector_id=self.connector_id),
            collection_id=document.collection_id,
        )
        return DocumentRef(collection=collection, document_id=document.document_id)
