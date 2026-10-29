"""One activity per connector checkpoint operation."""

from ccf_api_models.models import (
    DeleteCustomConnectorCheckpointRequest,
    GetCustomConnectorCheckpointRequest,
    GetCustomConnectorCheckpointResponseContent,
    PutCustomConnectorCheckpointRequest,
)

from common.operation import OperationRequest
from common.storage.ddb.custom_connectors_dao import (
    CustomConnectorsDao,
    DeleteCheckpointRequest,
    GetCheckpointRequest,
    PutCheckpointRequest,
)


class PutCustomConnectorCheckpointActivity:
    """Replace one connector checkpoint."""

    def __init__(self, dao: CustomConnectorsDao) -> None:
        self._dao = dao

    def put(self, request: OperationRequest[PutCustomConnectorCheckpointRequest]) -> None:
        """Persist checkpoint JSON at the tenant-scoped connector."""
        self._dao.put_checkpoint(
            PutCheckpointRequest(
                request.tenant,
                request.payload.connector_id,
                request.payload.checkpoint_data.get_secret_value(),
            )
        )


class GetCustomConnectorCheckpointActivity:
    """Fetch one connector checkpoint."""

    def __init__(self, dao: CustomConnectorsDao) -> None:
        self._dao = dao

    def fetch(
        self,
        request: OperationRequest[GetCustomConnectorCheckpointRequest],
    ) -> GetCustomConnectorCheckpointResponseContent:
        """Return an authorized checkpoint through its generated response."""
        checkpoint = self._dao.get_checkpoint(GetCheckpointRequest(request.tenant, request.payload.connector_id))
        return GetCustomConnectorCheckpointResponseContent(checkpoint=checkpoint)


class DeleteCustomConnectorCheckpointActivity:
    """Delete one connector checkpoint."""

    def __init__(self, dao: CustomConnectorsDao) -> None:
        self._dao = dao

    def delete(self, request: OperationRequest[DeleteCustomConnectorCheckpointRequest]) -> None:
        """Delete the tenant-scoped checkpoint."""
        self._dao.delete_checkpoint(DeleteCheckpointRequest(request.tenant, request.payload.connector_id))
