"""DynamoDB records of collections and their checkpoints (`CcfCollections`).

Collections are keyed by custom connector ARN and collection ID. Only batch put and
checkpoint put create a collection's item; every other operation reads or changes an
existing one. `deleting_at` marks a collection that a batch collection delete is removing,
and document writes check it in their transactions through `write_guard`.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from ccf_api_models.base import ValueModel
from ccf_api_models.contracts import TablesContract
from ccf_api_models.identifiers import CollectionRef, ConnectorRef
from ccf_api_models.models import Checkpoint, Collection, ConnectorStatus, GetCustomConnectorCheckpointResponseContent
from pydantic import BaseModel, ConfigDict, SecretStr

from common.aws_error_code import AwsErrorCode
from common.errors import CancellationReason, ResourceConflictError, ResourceNotFoundError, TransactionCanceledError
from common.resource_data_codec import DecryptResourceDataRequest, EncryptResourceDataRequest, ResourceDataCodec
from common.resource_ids import Clock
from common.storage.ddb.model_table import (
    AttributeValue,
    ConditionCheckRequest,
    DeleteItemRequest,
    ItemKey,
    ListItemsRequest,
    ModelTable,
    OnConditionFailure,
    Page,
    QueryRequest,
    SortCondition,
    SortOperator,
    TransactionRequest,
    TransactWrite,
    UpdateItemRequest,
)

_CONNECTOR_KEY = TablesContract.load().connectors.partition_key.name
"""A condition on it fails exactly when the custom connector's item doesn't exist."""


class _StoredCollection(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, val_json_bytes="base64")

    collection_id: str
    source_collection_id: str | None = None
    encrypted_checkpoint: bytes | None = None
    checkpoint_created_at: float | None = None
    checkpoint_updated_at: float | None = None
    deleting_at: float | None = None
    created_at: float
    updated_at: float


class _ExistingUpdate(ValueModel):
    """An update of an item that must exist; a missing one raises ResourceNotFoundError."""

    collection: CollectionRef
    action: str
    expression: str
    values: dict[str, AttributeValue] = {}


class CollectionWriteCondition(StrEnum):
    """What a document transaction requires of its collection's item."""

    EXISTS_AND_NOT_DELETING = "attribute_exists(collection_id) AND attribute_not_exists(deleting_at)"
    NOT_DELETING = "attribute_not_exists(deleting_at)"


class WriteGuardRequest(ValueModel):
    collection: CollectionRef
    condition: CollectionWriteCondition


class UpsertCollectionRequest(ValueModel):
    """A collection to create if it doesn't exist, and its source ID for a collection other than `col-default`."""

    collection: CollectionRef
    source_collection_id: str | None = None


class PutCheckpointRecord(ValueModel):
    """A collection's replacement checkpoint, already checked for size, and its source ID."""

    collection: CollectionRef
    source_collection_id: str | None
    checkpoint_data: SecretStr


class _CheckpointWrite(ValueModel):
    """A checkpoint update, and the custom connector status that stops it."""

    collection: CollectionRef
    blocking_status: ConnectorStatus
    conflict: str
    update: UpdateItemRequest


class _ConnectorStatusItem(BaseModel):
    """The custom connector item, read only for its status in a failed condition check."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    status: ConnectorStatus


class CollectionsTable(ValueModel):
    """The boto3 `CcfCollections` and custom connectors tables, the DynamoDB client, and what the DAO writes with."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    table: Any
    connectors_table: Any
    client: Any
    codec: ResourceDataCodec
    clock: Clock


class CollectionsDao:
    """Reads and writes collection items; it writes `CcfCollections` only, checking the custom connector's item."""

    def __init__(
        self,
        table: ModelTable[_StoredCollection],
        connectors: ModelTable[_ConnectorStatusItem],
        codec: ResourceDataCodec,
        clock: Clock,
    ) -> None:
        self._table = table
        self._connectors = connectors
        self._codec = codec
        self._clock = clock

    @classmethod
    def over(cls, request: CollectionsTable) -> CollectionsDao:
        contracts = TablesContract.load()
        return cls(
            ModelTable(request.table, request.client, _StoredCollection, contracts.collections),
            ModelTable(request.connectors_table, request.client, _ConnectorStatusItem, contracts.connectors),
            request.codec,
            request.clock,
        )

    def upsert(self, request: UpsertCollectionRequest) -> None:
        """Create the collection's item if it doesn't exist; a stored `source_collection_id` is never rewritten."""
        sets = ["created_at = if_not_exists(created_at, :now)", "updated_at = :now"]
        values: dict[str, AttributeValue] = {":now": self._clock.now()}
        if request.source_collection_id is not None:
            sets.append("source_collection_id = if_not_exists(source_collection_id, :source)")
            values[":source"] = request.source_collection_id
        self._table.update(
            UpdateItemRequest(
                key=_key(request.collection),
                action="Collection upsert",
                expression=f"SET {', '.join(sets)}",
                values=values,
            )
        )

    def is_deleting(self, collection: CollectionRef) -> bool:
        """Whether a batch collection delete has marked the collection, from a strongly consistent read."""
        stored = self._table.get(_key(collection))
        return stored is not None and stored.deleting_at is not None

    def write_guard(self, request: WriteGuardRequest) -> TransactWrite:
        """The `ConditionCheck` a document transaction includes, so no batch lands in a collection being deleted."""
        return self._table.condition_check(
            ConditionCheckRequest(key=_key(request.collection), condition=request.condition.value)
        )

    def mark_deleting(self, collection: CollectionRef) -> bool:
        """Mark an existing collection; False when there is no item, and none is created."""
        try:
            self._update_existing(
                _ExistingUpdate(
                    collection=collection,
                    action="Collection deletion mark",
                    expression="SET deleting_at = :now",
                    values={":now": self._clock.now()},
                )
            )
        except ResourceNotFoundError:
            return False
        return True

    def unmark_deleting(self, collection: CollectionRef) -> None:
        """Remove the mark; a collection already deleted stays deleted instead of coming back as a bare key."""
        try:
            self._update_existing(
                _ExistingUpdate(
                    collection=collection, action="Collection deletion unmark", expression="REMOVE deleting_at"
                )
            )
        except ResourceNotFoundError:
            return

    def delete_marked(self, collection: CollectionRef) -> None:
        """Delete the collection only while it is marked; a missing or unmarked item is left alone."""
        try:
            self._table.delete(
                DeleteItemRequest(
                    key=_key(collection), action="Collection deletion", condition="attribute_exists(deleting_at)"
                )
            )
        except ResourceConflictError:
            return

    def list(self, request: ListItemsRequest[ConnectorRef]) -> Page[Collection]:
        """One page of the custom connector's collections with their checkpoints, or the one named collection."""
        collection_id = request.filter_value
        page = self._table.query(
            QueryRequest(
                partition_value=request.parent.arn,
                sort_condition=None
                if collection_id is None
                else SortCondition(operator=SortOperator.EQUALS, value=collection_id),
                max_results=request.max_results,
                next_token=request.next_token,
            )
        )
        collections = [
            Collection(
                collection_id=stored.collection_id,
                source_collection_id=stored.source_collection_id,
                checkpoint_data=None
                if stored.encrypted_checkpoint is None
                else self._codec.decrypt(
                    DecryptResourceDataRequest(
                        ciphertext=stored.encrypted_checkpoint,
                        resource_arn=CollectionRef(connector=request.parent, collection_id=stored.collection_id).arn,
                    )
                ),
                created_at=stored.created_at,
                updated_at=stored.updated_at,
            )
            for stored in page.items
        ]
        return Page(items=collections, next_token=page.next_token)

    def put_checkpoint(self, record: PutCheckpointRecord) -> None:
        """Store the checkpoint, creating the collection's item, unless the custom connector is missing or deleting."""
        ciphertext = self._codec.encrypt(
            EncryptResourceDataRequest(data=record.checkpoint_data, resource_arn=record.collection.arn)
        )
        sets = [
            "encrypted_checkpoint = :checkpoint",
            "checkpoint_created_at = if_not_exists(checkpoint_created_at, :now)",
            "checkpoint_updated_at = :now",
            "created_at = if_not_exists(created_at, :now)",
            "updated_at = :now",
        ]
        values: dict[str, AttributeValue] = {":checkpoint": ciphertext, ":now": self._clock.now()}
        if record.source_collection_id is not None:
            sets.append("source_collection_id = if_not_exists(source_collection_id, :source)")
            values[":source"] = record.source_collection_id
        self._write(
            _CheckpointWrite(
                collection=record.collection,
                blocking_status=ConnectorStatus.DELETING,
                conflict="The custom connector is being deleted",
                update=UpdateItemRequest(
                    key=_key(record.collection),
                    action="Checkpoint write",
                    expression=f"SET {', '.join(sets)}",
                    values=values,
                ),
            )
        )

    def get_checkpoint(self, collection: CollectionRef) -> GetCustomConnectorCheckpointResponseContent:
        """The collection's checkpoint, with `checkpoint` unset when none is stored."""
        stored = self._table.get(_key(collection))
        if (
            stored is None
            or stored.encrypted_checkpoint is None
            or stored.checkpoint_created_at is None
            or stored.checkpoint_updated_at is None
        ):
            return GetCustomConnectorCheckpointResponseContent()
        return GetCustomConnectorCheckpointResponseContent(
            checkpoint=Checkpoint(
                checkpoint_data=self._codec.decrypt(
                    DecryptResourceDataRequest(ciphertext=stored.encrypted_checkpoint, resource_arn=collection.arn)
                ),
                created_at=stored.checkpoint_created_at,
                updated_at=stored.checkpoint_updated_at,
            )
        )

    def delete_checkpoint(self, collection: CollectionRef) -> None:
        """Remove the checkpoint unless a job runs; a missing item or checkpoint is already deleted."""
        self._write(
            _CheckpointWrite(
                collection=collection,
                blocking_status=ConnectorStatus.IN_USE,
                conflict="A job of the custom connector is running",
                update=UpdateItemRequest(
                    key=_key(collection),
                    action="Checkpoint deletion",
                    expression="SET updated_at = :now REMOVE encrypted_checkpoint, checkpoint_created_at, "
                    "checkpoint_updated_at",
                    condition="attribute_exists(collection_id)",
                    values={":now": self._clock.now()},
                ),
            )
        )

    def _write(self, write: _CheckpointWrite) -> None:
        """Check the custom connector and update the collection in one transaction."""
        connector = write.collection.connector
        check = self._connectors.condition_check(
            ConditionCheckRequest(
                key=ItemKey(partition=connector.tenant.arn_prefix, sort=connector.destination_connector_key),
                condition=f"attribute_exists({_CONNECTOR_KEY}) AND #status <> :blocking",
                names={"#status": "status"},
                values={":blocking": write.blocking_status.value},
                on_condition_failure=OnConditionFailure.ALL_OLD,
            )
        )
        update = self._table.update_action(write.update)
        try:
            self._table.transact(TransactionRequest(action=write.update.action, writes=(check, update)))
        except TransactionCanceledError as error:
            connector_reason, collection_reason = error.reasons
            match connector_reason:
                case CancellationReason(code=AwsErrorCode.CONDITIONAL_CHECK_FAILED_REASON, item=None):
                    raise ResourceNotFoundError("The custom connector does not exist") from error
                case CancellationReason(code=AwsErrorCode.CONDITIONAL_CHECK_FAILED_REASON):
                    raise ResourceConflictError(write.conflict) from error
            if collection_reason.code is AwsErrorCode.CONDITIONAL_CHECK_FAILED_REASON:
                return
            raise

    def _update_existing(self, update: _ExistingUpdate) -> None:
        self._table.update(
            UpdateItemRequest(
                key=_key(update.collection),
                action=update.action,
                expression=update.expression,
                condition="attribute_exists(collection_id)",
                values=update.values,
            )
        )


def _key(collection: CollectionRef) -> ItemKey:
    return ItemKey(partition=collection.connector.arn, sort=collection.collection_id)
