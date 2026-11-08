"""The batch document operations.

A client writes each document's file and metadata file itself, through its source-access role,
and then records them. A batch delete removes the files, through the tenant's destination role,
before their records, so a failure never leaves a file the knowledge base reads without a record
that names it.
"""

from __future__ import annotations

from ccf_api_models.identifiers import CollectionRef, ConnectorRef
from ccf_api_models.models import BatchDeleteCustomConnectorDocumentsRequest, BatchPutCustomConnectorDocumentsRequest

from common.errors import ResourceConflictError
from common.observability import MetricDimensionName, MetricName, MetricRecord, ServiceMetrics
from common.operation import OperationRequest
from common.storage.ddb.collections_dao import (
    CollectionsDao,
    CollectionWriteCondition,
    UpsertCollectionRequest,
    WriteGuardRequest,
)
from common.storage.ddb.documents_dao import DeleteDocumentsRecord, DocumentsDao, PutDocumentsRecord
from common.storage.s3.destination_files_dao import DeleteDocumentFilesRequest, DestinationFilesDao
from common.validators.connector_write_gate import ConnectorWriteGate
from common.validators.source_identifiers import CollectionIdentifierValidator, DocumentIdentifierValidator


class BatchPutCustomConnectorDocumentsActivity:
    """Records up to 99 documents whose files the client already wrote."""

    def __init__(
        self,
        gate: ConnectorWriteGate,
        collections: CollectionsDao,
        documents: DocumentsDao,
        collection_identifiers: CollectionIdentifierValidator,
        document_identifiers: DocumentIdentifierValidator,
        metrics: ServiceMetrics,
    ) -> None:
        self._gate = gate
        self._collections = collections
        self._documents = documents
        self._collection_identifiers = collection_identifiers
        self._document_identifiers = document_identifiers
        self._metrics = metrics

    def put(self, request: OperationRequest[BatchPutCustomConnectorDocumentsRequest]) -> None:
        """The transaction's guard rejects a collection being deleted, so the put reads no collection."""
        payload = request.payload
        collection_id = self._collection_identifiers.validate(payload)
        self._document_identifiers.validate(payload)
        self._gate.check(request)
        connector = ConnectorRef.of(request)
        collection = CollectionRef(connector=connector, collection_id=collection_id)
        self._collections.upsert(
            UpsertCollectionRequest(collection=collection, source_collection_id=payload.source_collection_id)
        )
        guard = self._collections.write_guard(
            WriteGuardRequest(collection=collection, condition=CollectionWriteCondition.EXISTS_AND_NOT_DELETING)
        )
        self._documents.put_documents(
            PutDocumentsRecord(collection=collection, documents=tuple(payload.documents), guard=guard)
        )
        self._metrics.record(
            MetricRecord(
                name=MetricName.DOCUMENTS_RECORDED,
                value=len(payload.documents),
                dimensions={MetricDimensionName.CONNECTOR_ID: connector.connector_id},
            )
        )


class BatchDeleteCustomConnectorDocumentsActivity:
    """Deletes up to 99 documents' files and then their records; unknown document IDs are ignored."""

    def __init__(
        self,
        gate: ConnectorWriteGate,
        collections: CollectionsDao,
        documents: DocumentsDao,
        files: DestinationFilesDao,
        metrics: ServiceMetrics,
    ) -> None:
        self._gate = gate
        self._collections = collections
        self._documents = documents
        self._files = files
        self._metrics = metrics

    def delete(self, request: OperationRequest[BatchDeleteCustomConnectorDocumentsRequest]) -> None:
        payload = request.payload
        document_ids = tuple(payload.document_ids)
        destination = self._gate.check(request)
        connector = ConnectorRef.of(request)
        collection = CollectionRef(connector=connector, collection_id=payload.collection_id)
        if self._collections.is_deleting(collection):
            raise ResourceConflictError("Collection is being deleted")
        bucket_name = destination.destination_resources.bucket_name
        if bucket_name is None:
            raise ResourceConflictError("Destination has no bucket")
        self._files.delete_document_files(
            DeleteDocumentFilesRequest(
                collection=collection,
                role_arn=destination.role_arn,
                bucket_name=bucket_name,
                document_ids=document_ids,
            )
        )
        guard = self._collections.write_guard(
            WriteGuardRequest(collection=collection, condition=CollectionWriteCondition.NOT_DELETING)
        )
        self._documents.delete_documents(
            DeleteDocumentsRecord(collection=collection, document_ids=document_ids, guard=guard)
        )
        self._metrics.record(
            MetricRecord(
                name=MetricName.DOCUMENTS_DELETED,
                value=len(document_ids),
                dimensions={MetricDimensionName.CONNECTOR_ID: connector.connector_id},
            )
        )
