"""The batch collection delete."""

from __future__ import annotations

from ccf_api_models.identifiers import CollectionRef, ConnectorRef
from ccf_api_models.models import BatchDeleteCustomConnectorCollectionsRequest

from common.errors import ResourceConflictError
from common.observability import MetricDimensionName, MetricName, MetricRecord, ServiceMetrics, logger
from common.operation import OperationRequest
from common.storage.ddb.collections_dao import CollectionsDao
from common.storage.ddb.documents_dao import DocumentsDao


class BatchDeleteCustomConnectorCollectionsActivity:
    """Deletes up to 100 empty collections, all or none; a collection that still holds documents conflicts.

    Marking each collection first makes concurrent document writes fail, so no document can land
    between the emptiness check and the delete. When any collection still holds documents, or any
    step fails, every mark this call set is removed and the original error is raised.
    """

    def __init__(self, collections: CollectionsDao, documents: DocumentsDao, metrics: ServiceMetrics) -> None:
        self._collections = collections
        self._documents = documents
        self._metrics = metrics

    def delete(self, request: OperationRequest[BatchDeleteCustomConnectorCollectionsRequest]) -> None:
        connector = ConnectorRef.of(request)
        targets = [
            CollectionRef(connector=connector, collection_id=collection.collection_id)
            for collection in request.payload.collections
        ]
        marked: list[CollectionRef] = []
        try:
            for collection in targets:
                if self._collections.mark_deleting(collection):
                    marked.append(collection)
            if any(self._documents.has_documents(collection) for collection in marked):
                raise ResourceConflictError("A collection still holds documents")
            for collection in marked:
                self._collections.delete_marked(collection)
        except Exception:
            for collection in marked:
                self._unmark(collection)
            raise
        self._metrics.record(
            MetricRecord(
                name=MetricName.COLLECTIONS_DELETED,
                value=len(targets),
                dimensions={MetricDimensionName.CONNECTOR_ID: connector.connector_id},
            )
        )

    def _unmark(self, collection: CollectionRef) -> None:
        """Remove one mark; a failed unmark is logged so the others still run."""
        try:
            self._collections.unmark_deleting(collection)
        except Exception as error:
            logger.error(
                "collection_unmark_failed",
                extra={
                    "connector_id": collection.connector.connector_id,
                    "collection_id": collection.collection_id,
                    "error_type": type(error).__name__,
                },
            )
