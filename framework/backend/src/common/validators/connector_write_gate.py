"""Document writes need an `ACTIVE` destination and a custom connector that isn't being deleted."""

from __future__ import annotations

from typing import Any

from ccf_api_models.identifiers import ConnectorRef
from ccf_api_models.models import ConnectorStatus, DestinationStatus

from common.errors import InternalServiceError, ResourceConflictError
from common.operation import OperationRequest
from common.storage.ddb.destinations_dao import DestinationsDao, StoredDestination


class ConnectorWriteGate:
    """Reads the destination once; the custom connector is the item the tenant resolver read for the runtime role."""

    def __init__(self, destinations: DestinationsDao) -> None:
        self._destinations = destinations

    def check(self, request: OperationRequest[Any]) -> StoredDestination:
        """Return the destination, or raise ResourceConflictError."""
        if request.connector is None:
            raise InternalServiceError("A document write reached the gate without its custom connector")
        destination = self._destinations.get(ConnectorRef.of(request).destination)
        if destination.status is not DestinationStatus.ACTIVE:
            raise ResourceConflictError("Destination is not ACTIVE")
        if request.connector.status is ConnectorStatus.DELETING:
            raise ResourceConflictError("Custom connector is being deleted")
        return destination
