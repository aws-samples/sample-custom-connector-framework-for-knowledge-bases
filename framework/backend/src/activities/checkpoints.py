"""One activity per checkpoint operation.

A checkpoint is a string the connector implementation keeps for one collection. CCF checks
only its size and never parses it. `CollectionsDao` checks the custom connector's item in the
same transaction as the write.
"""

from __future__ import annotations

from ccf_api_models.identifiers import CollectionRef, ConnectorRef
from ccf_api_models.models import (
    DeleteCustomConnectorCheckpointRequest,
    GetCustomConnectorCheckpointRequest,
    GetCustomConnectorCheckpointResponseContent,
    PutCustomConnectorCheckpointRequest,
)

from common.operation import OperationRequest
from common.storage.ddb.collections_dao import CollectionsDao, PutCheckpointRecord
from common.validators.byte_limits import FieldOfModel, Utf8ByteLimit
from common.validators.source_identifiers import CollectionIdentifierValidator

CHECKPOINT_DATA_LIMIT = Utf8ByteLimit.of(
    FieldOfModel(model=PutCustomConnectorCheckpointRequest, field_name="checkpoint_data")
)


class PutCustomConnectorCheckpointActivity:
    """Stores a collection's checkpoint, creating the collection's item when it doesn't exist."""

    def __init__(self, collections: CollectionsDao, identifiers: CollectionIdentifierValidator) -> None:
        self._collections = collections
        self._identifiers = identifiers

    def put(self, request: OperationRequest[PutCustomConnectorCheckpointRequest]) -> None:
        payload = request.payload
        collection_id = self._identifiers.validate(payload)
        CHECKPOINT_DATA_LIMIT.check(payload.checkpoint_data)
        self._collections.put_checkpoint(
            PutCheckpointRecord(
                collection=CollectionRef(connector=ConnectorRef.of(request), collection_id=collection_id),
                source_collection_id=payload.source_collection_id,
                checkpoint_data=payload.checkpoint_data,
            )
        )


class GetCustomConnectorCheckpointActivity:
    """Returns a collection's checkpoint; the response has none when none is stored."""

    def __init__(self, collections: CollectionsDao) -> None:
        self._collections = collections

    def fetch(
        self, request: OperationRequest[GetCustomConnectorCheckpointRequest]
    ) -> GetCustomConnectorCheckpointResponseContent:
        collection = CollectionRef(connector=ConnectorRef.of(request), collection_id=request.payload.collection_id)
        return self._collections.get_checkpoint(collection)


class DeleteCustomConnectorCheckpointActivity:
    """Removes a collection's checkpoint so the next job reads the collection from the start."""

    def __init__(self, collections: CollectionsDao) -> None:
        self._collections = collections

    def delete(self, request: OperationRequest[DeleteCustomConnectorCheckpointRequest]) -> None:
        collection = CollectionRef(connector=ConnectorRef.of(request), collection_id=request.payload.collection_id)
        self._collections.delete_checkpoint(collection)
