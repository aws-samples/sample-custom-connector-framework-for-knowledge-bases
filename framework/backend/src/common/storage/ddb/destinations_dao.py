"""Tenant-scoped DynamoDB records of destinations (`CcfDestinations`).

The destination workflow writes `status`, `status_reason`, and `destination_resources` itself.
"""

from __future__ import annotations

from typing import Any

from ccf_api_models.base import ValueModel
from ccf_api_models.contracts import TablesContract
from ccf_api_models.identifiers import DestinationRef, TenantContext
from ccf_api_models.models import (
    CreatedDestination,
    CreateDestinationRequest,
    Destination,
    DestinationResources,
    DestinationStatus,
    DestinationSummary,
    MetadataField,
)
from pydantic import ConfigDict

from common.errors import ResourceConflictError, ResourceNotFoundError
from common.resource_ids import Clock
from common.storage.ddb.model_table import (
    AttributeValue,
    DeleteItemRequest,
    ItemKey,
    ListItemsRequest,
    ModelTable,
    Page,
    PutItemRequest,
    QueryRequest,
    SortCondition,
    SortOperator,
    UpdateItemRequest,
)

BY_NAME_INDEX = "ByName"
PARTITION_KEY = "tenant_arn_prefix"
SORT_KEY = "destination_id"
"""The key names the custom connector and job DAOs' transactions still write by hand."""
_STATUS = {"#status": "status"}


class StoredDestination(Destination):
    """A CcfDestinations item: the API view plus the members only the backend reads."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    tenant_arn_prefix: str
    correlation_id: str | None = None
    workflow_execution_arn: str | None = None
    previous_status: DestinationStatus | None = None
    ingestion_pending: str | None = None

    def destination(self) -> Destination:
        return Destination.model_validate(self.model_dump(include=set(Destination.model_fields)))

    def created(self) -> CreatedDestination:
        return CreatedDestination.model_validate(
            self.model_dump(include=set(CreatedDestination.model_fields), exclude_none=True)
        )


class DestinationSummaryItem(DestinationSummary):
    """A destination as the `ByName` index projects it: the summary members and the keys."""

    model_config = ConfigDict(frozen=True, extra="ignore")


class CreateDestinationRecord(ValueModel):
    """A validated create request and the identifiers the API assigned to it."""

    destination: DestinationRef
    payload: CreateDestinationRequest
    correlation_id: str


class WorkflowExecutionRecord(ValueModel):
    """The destination workflow execution started for a destination."""

    destination: DestinationRef
    execution_arn: str


class IngestionTokenRequest(ValueModel):
    destination: DestinationRef
    token: str


class MetadataFieldsChange(ValueModel):
    """The custom metadata fields that replace an `ACTIVE` destination's."""

    destination: DestinationRef
    fields: tuple[MetadataField, ...]


class _Transition(ValueModel):
    """One conditional update: the action that names its error, and its expressions."""

    action: str
    expression: str
    condition: str
    values: dict[str, AttributeValue] = {}


class _Change(ValueModel):
    destination: DestinationRef
    transition: _Transition
    values: dict[str, AttributeValue] = {}


_DESTINATION_EXISTS = "attribute_exists(destination_id)"
_RECORD_EXECUTION = _Transition(
    action="Destination workflow record",
    expression="SET workflow_execution_arn = :arn, updated_at = :now",
    condition=_DESTINATION_EXISTS,
)
_BEGIN_DELETION = _Transition(
    action="Destination deletion start",
    expression="SET previous_status = #status, #status = :deleting, updated_at = :now",
    condition="#status IN (:active, :failed)",
    values={
        ":deleting": DestinationStatus.DELETING.value,
        ":active": DestinationStatus.ACTIVE.value,
        ":failed": DestinationStatus.FAILED.value,
    },
)
_RESTORE_STATUS = _Transition(
    action="Destination status restore",
    expression="SET #status = previous_status, updated_at = :now REMOVE previous_status",
    condition="#status = :deleting AND attribute_exists(previous_status)",
    values={":deleting": DestinationStatus.DELETING.value},
)
_CLEAR_PENDING = _Transition(
    action="Ingestion request clear",
    expression="REMOVE ingestion_pending",
    condition="ingestion_pending = :token",
)
_RESTORE_PENDING = _Transition(
    action="Ingestion request restore",
    expression="SET ingestion_pending = :token",
    condition=f"{_DESTINATION_EXISTS} AND attribute_not_exists(ingestion_pending)",
)
_REPLACE_METADATA_FIELDS = _Transition(
    action="Destination metadata fields update",
    expression="SET metadata_fields = :fields, updated_at = :now",
    condition="#status = :active",
    values={":active": DestinationStatus.ACTIVE.value},
)


class DestinationsTable(ValueModel):
    """The boto3 `CcfDestinations` table and the DynamoDB client its transactions use."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    table: Any
    client: Any
    clock: Clock


class DestinationsDao:
    """Reads and writes destination records; every single-item read is strongly consistent."""

    def __init__(
        self,
        table: ModelTable[StoredDestination],
        summaries: ModelTable[DestinationSummaryItem],
        clock: Clock,
    ) -> None:
        self._table = table
        self._summaries = summaries
        self._clock = clock

    @classmethod
    def over(cls, request: DestinationsTable) -> DestinationsDao:
        """The DAO over one table: full items for reads and writes, summaries for the `ByName` index."""
        contract = TablesContract.load().destinations
        return cls(
            ModelTable(request.table, request.client, StoredDestination, contract),
            ModelTable(request.table, request.client, DestinationSummaryItem, contract),
            request.clock,
        )

    def create(self, record: CreateDestinationRecord) -> StoredDestination:
        """Save a new destination in `CREATING_BUCKET`."""
        now = float(self._clock.now())
        payload = record.payload
        item = StoredDestination(
            tenant_arn_prefix=record.destination.tenant.arn_prefix,
            destination_id=record.destination.destination_id,
            arn=record.destination.arn,
            name=payload.name,
            description=payload.description,
            type=payload.type,
            access_control_enabled=bool(payload.access_control_enabled),
            status=DestinationStatus.CREATING_BUCKET,
            role_arn=payload.role_arn,
            destination_resources=DestinationResources(),
            correlation_id=record.correlation_id,
            created_at=now,
            updated_at=now,
        )
        return self._table.put(
            PutItemRequest(item=item, action="Destination creation", condition="attribute_not_exists(destination_id)")
        )

    def get(self, destination: DestinationRef) -> StoredDestination:
        """Return the destination, or raise ResourceNotFoundError."""
        return self._table.require(_key(destination))

    def exists(self, destination: DestinationRef) -> bool:
        return self._table.get(_key(destination)) is not None

    def list(self, request: ListItemsRequest[TenantContext]) -> Page[DestinationSummary]:
        """One page of the tenant's destination summaries, from the `ByName` index when a name is given."""
        name = request.filter_value
        page = self._summaries.query(
            QueryRequest(
                partition_value=request.parent.arn_prefix,
                index=None if name is None else BY_NAME_INDEX,
                sort_condition=None if name is None else SortCondition(operator=SortOperator.EQUALS, value=name),
                max_results=request.max_results,
                next_token=request.next_token,
            )
        )
        summaries = [DestinationSummary.model_validate(item.model_dump()) for item in page.items]
        return Page(items=summaries, next_token=page.next_token)

    def record_workflow_execution(self, record: WorkflowExecutionRecord) -> None:
        """Store the ARN of the execution most recently started for the destination."""
        self._change(
            _Change(destination=record.destination, transition=_RECORD_EXECUTION, values={":arn": record.execution_arn})
        )

    def delete_unstarted(self, destination: DestinationRef) -> None:
        """Delete a destination whose workflow never started, so a retry with the same client token creates it."""
        self._table.delete(
            DeleteItemRequest(
                key=_key(destination),
                action="Destination deletion",
                condition="#status = :creating AND attribute_not_exists(workflow_execution_arn)",
                names=_STATUS,
                values={":creating": DestinationStatus.CREATING_BUCKET.value},
            )
        )

    def begin_deletion(self, destination: DestinationRef) -> StoredDestination:
        """Set an `ACTIVE` or `FAILED` destination to `DELETING`, keeping its status in `previous_status`."""
        return self._change(_Change(destination=destination, transition=_BEGIN_DELETION))

    def restore_previous_status(self, destination: DestinationRef) -> None:
        """Return a `DELETING` destination to the status it had before the deletion started."""
        self._change(_Change(destination=destination, transition=_RESTORE_STATUS))

    def replace_metadata_fields(self, change: MetadataFieldsChange) -> StoredDestination:
        """Store the fields of an `ACTIVE` destination; any other status raises ResourceConflictError."""
        fields = [field.model_dump(mode="json") for field in change.fields]
        return self._change(
            _Change(destination=change.destination, transition=_REPLACE_METADATA_FIELDS, values={":fields": fields})
        )

    def clear_ingestion_pending(self, request: IngestionTokenRequest) -> bool:
        """Remove the token if it is the one waiting, and return whether it was."""
        change = _Change(destination=request.destination, transition=_CLEAR_PENDING, values={":token": request.token})
        try:
            self._change(change)
        except ResourceConflictError, ResourceNotFoundError:
            return False
        return True

    def restore_ingestion_pending(self, request: IngestionTokenRequest) -> None:
        """Put the token back unless another one is waiting or the destination is gone."""
        change = _Change(destination=request.destination, transition=_RESTORE_PENDING, values={":token": request.token})
        try:
            self._change(change)
        except ResourceConflictError, ResourceNotFoundError:
            return

    def _change(self, change: _Change) -> StoredDestination:
        """One conditional update; `:now` and `#status` are sent only when the expressions use them."""
        transition = change.transition
        text = f"{transition.expression} {transition.condition}"
        now = {":now": self._clock.now()} if ":now" in text else {}
        return self._table.update(
            UpdateItemRequest(
                key=_key(change.destination),
                action=transition.action,
                expression=transition.expression,
                condition=transition.condition,
                values={**transition.values, **change.values, **now},
                names=_STATUS if "#status" in text else {},
            )
        )


def _key(destination: DestinationRef) -> ItemKey:
    return ItemKey(partition=destination.tenant.arn_prefix, sort=destination.destination_id)
