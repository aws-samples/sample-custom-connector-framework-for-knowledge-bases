"""One activity per connector document-state operation."""

from ccf_api_models.models import (
    BatchDeleteCustomConnectorDocumentsRequest,
    BatchPutCustomConnectorDocumentsRequest,
    ListCustomConnectorDocumentsRequest,
    ListCustomConnectorDocumentsResponseContent,
)

from common.operation import OperationRequest
from common.storage.ddb.custom_connector_documents_dao import (
    BatchDeleteDocumentsRequest,
    BatchPutDocumentsRequest,
    CustomConnectorDocumentsDao,
    ListDocumentsRequest,
)


class BatchPutCustomConnectorDocumentsActivity:
    """Persist document state for one connector."""

    def __init__(self, dao: CustomConnectorDocumentsDao) -> None:
        self._dao = dao

    def put(self, request: OperationRequest[BatchPutCustomConnectorDocumentsRequest]) -> None:
        """Upsert generated document state models."""
        self._dao.batch_put_documents(
            BatchPutDocumentsRequest(
                request.tenant,
                request.payload.connector_id,
                request.payload.documents,
            )
        )


class BatchDeleteCustomConnectorDocumentsActivity:
    """Delete document state for one connector."""

    def __init__(self, dao: CustomConnectorDocumentsDao) -> None:
        self._dao = dao

    def delete(self, request: OperationRequest[BatchDeleteCustomConnectorDocumentsRequest]) -> None:
        """Delete generated document identifiers."""
        self._dao.batch_delete_documents(
            BatchDeleteDocumentsRequest(
                request.tenant,
                request.payload.connector_id,
                [document_id.root for document_id in request.payload.document_ids],
            )
        )


class ListCustomConnectorDocumentsActivity:
    """List document state for one connector."""

    def __init__(self, dao: CustomConnectorDocumentsDao) -> None:
        self._dao = dao

    def list(
        self,
        request: OperationRequest[ListCustomConnectorDocumentsRequest],
    ) -> ListCustomConnectorDocumentsResponseContent:
        """Return generated document summaries."""
        stored = self._dao.list_documents(
            ListDocumentsRequest(
                request.tenant,
                request.payload.connector_id,
                request.payload.max_results or 50,
                request.payload.next_token,
            )
        )
        return ListCustomConnectorDocumentsResponseContent(
            documents=stored.documents,
            next_token=stored.next_token,
        )
