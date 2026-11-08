"""Checks that collection and document IDs are the hashes of the source IDs sent with them."""

from __future__ import annotations

from ccf_api_models.identifiers import DEFAULT_COLLECTION_ID, CollectionIdentity, SourceIdKind
from ccf_api_models.models import BatchPutCustomConnectorDocumentsRequest, PutCustomConnectorCheckpointRequest

from common.errors import BadRequestError

type CollectionRequest = BatchPutCustomConnectorDocumentsRequest | PutCustomConnectorCheckpointRequest


class CollectionIdentifierValidator:
    """Returns the collection a request names, or raises BadRequestError naming `source_collection_id`."""

    def validate(self, request: CollectionRequest) -> str:
        if request.collection_id == DEFAULT_COLLECTION_ID and request.source_collection_id is not None:
            raise BadRequestError("The default collection takes no source ID", fields=("source_collection_id",))
        if CollectionIdentity.of(request.source_collection_id).collection_id != request.collection_id:
            raise BadRequestError("The collection ID isn't its source ID's hash", fields=("source_collection_id",))
        return request.collection_id


class DocumentIdentifierValidator:
    """Rejects a batch whose document IDs repeat or aren't the hashes of their source document IDs."""

    def validate(self, request: BatchPutCustomConnectorDocumentsRequest) -> None:
        documents = request.documents
        if len({document.document_id for document in documents}) != len(documents):
            raise BadRequestError("A document ID repeats", fields=("documents",))
        if any(document.document_id != SourceIdKind.DOCUMENT.of(document.source_document_id) for document in documents):
            raise BadRequestError("A document ID isn't its source ID's hash", fields=("document_id",))
