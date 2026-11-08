"""Tenant-scoped DynamoDB records of custom connectors (`CcfCustomConnectors`).

Custom connectors are keyed by tenant ARN prefix and `<destination_id>#<connector_id>`,
so a custom connector in another destination is never found. The `ByName` index keys them
by tenant and `<destination_id>#<name>` and projects only summary attributes, never the
configuration ciphertext. Checkpoints live on collection items, not here.
"""

from __future__ import annotations

from typing import Any

from ccf_api_models.base import ValueModel
from ccf_api_models.contracts import TablesContract
from ccf_api_models.identifiers import ConnectorRef, DestinationRef, TenantContext
from ccf_api_models.models import (
    Connector,
    ConnectorStatus,
    ConnectorSummary,
    ContainerProperties,
    CreateCustomConnectorRequest,
    DestinationStatus,
    LogDestination,
    SourceAccess,
    UpdateCustomConnectorRequest,
)
from pydantic import ConfigDict, Field, SecretStr, field_validator

from common.aws_error_code import AwsErrorCode
from common.errors import ResourceConflictError, TransactionCanceledError
from common.resource_ids import Clock
from common.runtime_role import RuntimeRoleAccess
from common.storage.ddb.destinations_dao import StoredDestination
from common.storage.ddb.model_table import (
    ConditionCheckRequest,
    ItemAttributesRequest,
    ItemKey,
    ListItemsRequest,
    ModelTable,
    Page,
    PutItemRequest,
    QueryRequest,
    ReadConsistency,
    SortCondition,
    SortOperator,
    TransactionRequest,
    UpdateItemRequest,
)

PARTITION_KEY = "tenant_arn_prefix"
SORT_KEY = "destination_connector_key"
"""The key names the jobs DAO's transaction still writes by hand."""
BY_NAME_INDEX = "ByName"
_LOG_GROUP_SEGMENT = ":log-group:"
_ALL_STREAMS = ":*"
_CLEARABLE = ("log_destination", "schedule")
"""The optional members an update removes when it sends them as null."""
_STATUS = {"#status": "status"}
_AVAILABLE = {":available": ConnectorStatus.AVAILABLE.value}


class ConnectorChange(ValueModel):
    """An update request and the replacement ciphertext when it sends a configuration."""

    payload: UpdateCustomConnectorRequest
    encrypted_configuration: bytes | None = None


class ConnectorItem(Connector):
    """A stored custom connector: the API's `Connector` without its plaintext configuration, plus its keys."""

    model_config = ConfigDict(extra="ignore", frozen=True, val_json_bytes="base64")

    connector_configuration: SecretStr | None = Field(default=None, exclude=True)
    tenant_arn_prefix: str
    destination_connector_key: str
    destination_name_key: str
    encrypted_configuration: bytes | None = None
    active_job_id: str | None = None
    deletion_execution_arn: str | None = None
    job_definition_arn: str | None = None
    """The custom connector's one active Batch job definition revision; jobs run it by name."""
    version: int

    @field_validator("log_destination")
    @classmethod
    def _stored_log_group_arn(cls, value: LogDestination | None) -> LogDestination | None:
        """The log group ARN without `:*`, so it is stored the same whether the caller sent the suffix or not."""
        return None if value is None else value.model_copy(update={"arn": value.arn.removesuffix(_ALL_STREAMS)})

    @property
    def ref(self) -> ConnectorRef:
        tenant = TenantContext.from_arn_prefix(self.tenant_arn_prefix)
        destination = DestinationRef(tenant=tenant, destination_id=self.destination_id)
        return ConnectorRef(destination=destination, connector_id=self.connector_id)

    @property
    def log_group_name(self) -> str | None:
        """The log group name in the log destination's ARN; None without a log destination."""
        return None if self.log_destination is None else self.log_destination.arn.split(_LOG_GROUP_SEGMENT, 1)[1]

    def runtime_role_access(self) -> RuntimeRoleAccess:
        """The tenant roles the custom connector's runtime role may assume."""
        return RuntimeRoleAccess(
            connector=self.ref,
            source_role_arn=self.source_access.role_arn,
            log_delivery_role_arn=None if self.log_destination is None else self.log_destination.role_arn,
        )

    def with_update(self, change: ConnectorChange) -> ConnectorItem:
        """The item after the update: each sent member replaces or merges, a clearable one sent as null clears."""
        payload = change.payload
        sent = payload.model_fields_set
        updates: dict[str, Any] = {name: getattr(payload, name) for name in _CLEARABLE if name in sent}
        if payload.name is not None:
            updates |= {"name": payload.name, "destination_name_key": f"{self.destination_id}#{payload.name}"}
        if payload.description is not None:
            updates["description"] = payload.description
        if payload.container_properties is not None:
            sent_container = payload.container_properties.model_dump(exclude_none=True)
            updates["container_properties"] = {**self.container_properties.model_dump(), **sent_container}
        if payload.source_access is not None:
            updates["source_access"] = SourceAccess(
                role_arn=payload.source_access.role_arn,
                secret_arn=payload.source_access.secret_arn,
                external_id=self.source_access.external_id,
            )
        if change.encrypted_configuration is not None:
            updates["encrypted_configuration"] = change.encrypted_configuration
        return type(self).model_validate({**self.model_dump(), **updates})


class ConnectorSummaryItem(ConnectorSummary):
    """A custom connector as the `ByName` index projects it: the summary members and the keys."""

    model_config = ConfigDict(frozen=True, extra="ignore")


class CreateConnectorRecord(ValueModel):
    """A validated create request and the values the API generated for it."""

    connector: ConnectorRef
    payload: CreateCustomConnectorRequest
    external_id: str
    runtime_role_arn: str
    encrypted_configuration: bytes | None = None
    job_definition_arn: str | None = None


class ConnectorUpdate(ValueModel):
    """The item an update read and the item it writes; the DAO writes the difference."""

    stored: ConnectorItem
    merged: ConnectorItem


class DeletionExecutionRecord(ValueModel):
    """The connector deletion workflow execution started for a custom connector."""

    connector: ConnectorRef
    execution_arn: str


class CustomConnectorsTables(ValueModel):
    """The boto3 connectors and destinations tables, the client their transactions use, and the clock."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    table: Any
    destinations_table: Any
    client: Any
    clock: Clock


class CustomConnectorsDao:
    """Reads and writes custom connector records; every single-item read is strongly consistent."""

    def __init__(
        self,
        table: ModelTable[ConnectorItem],
        summaries: ModelTable[ConnectorSummaryItem],
        destinations: ModelTable[StoredDestination],
        clock: Clock,
    ) -> None:
        self._table = table
        self._summaries = summaries
        self._destinations = destinations
        self._clock = clock

    @classmethod
    def over(cls, request: CustomConnectorsTables) -> CustomConnectorsDao:
        """The DAO over the connectors table, and the destinations table its create transaction checks."""
        contracts = TablesContract.load()
        return cls(
            ModelTable(request.table, request.client, ConnectorItem, contracts.connectors),
            ModelTable(request.table, request.client, ConnectorSummaryItem, contracts.connectors),
            ModelTable(request.destinations_table, request.client, StoredDestination, contracts.destinations),
            request.clock,
        )

    def create(self, record: CreateConnectorRecord) -> ConnectorItem:
        """Save a new `AVAILABLE` custom connector in one transaction that requires its destination `ACTIVE`."""
        item = self.new_item(record)
        destination = record.connector.destination
        try:
            self._table.transact(
                TransactionRequest(
                    action="Custom connector creation",
                    writes=(
                        self._table.put_action(
                            PutItemRequest(
                                item=item,
                                action="Custom connector creation",
                                condition=f"attribute_not_exists({SORT_KEY})",
                            )
                        ),
                        self._destinations.condition_check(
                            ConditionCheckRequest(
                                key=ItemKey(partition=destination.tenant.arn_prefix, sort=destination.destination_id),
                                condition="#status = :active",
                                names=_STATUS,
                                values={":active": DestinationStatus.ACTIVE.value},
                            )
                        ),
                    ),
                )
            )
        except TransactionCanceledError as error:
            connector_exists, destination_inactive = (
                reason.code is AwsErrorCode.CONDITIONAL_CHECK_FAILED_REASON for reason in error.reasons
            )
            if connector_exists:
                raise ResourceConflictError("A custom connector with this ID already exists") from error
            if destination_inactive:
                raise ResourceConflictError("Destination is not ACTIVE") from error
            raise
        return item

    def get(self, connector: ConnectorRef) -> ConnectorItem:
        """Return the custom connector, or raise ResourceNotFoundError."""
        return self._table.require(_key(connector))

    def exists(self, connector: ConnectorRef) -> bool:
        return self._table.get(_key(connector)) is not None

    def list(self, request: ListItemsRequest[DestinationRef]) -> Page[ConnectorSummary]:
        """One page of the destination's custom connector summaries, from the `ByName` index when a name is given."""
        prefix = f"{request.parent.destination_id}#"
        name = request.filter_value
        page = self._summaries.query(
            QueryRequest(
                partition_value=request.parent.tenant.arn_prefix,
                index=None if name is None else BY_NAME_INDEX,
                sort_condition=SortCondition(operator=SortOperator.BEGINS_WITH, value=prefix)
                if name is None
                else SortCondition(operator=SortOperator.EQUALS, value=f"{prefix}{name}"),
                max_results=request.max_results,
                next_token=request.next_token,
            )
        )
        summaries = [ConnectorSummary.model_validate(item.model_dump()) for item in page.items]
        return Page(items=summaries, next_token=page.next_token)

    def has_connectors(self, destination: DestinationRef) -> bool:
        """Return whether any custom connector remains in the destination, from a strongly consistent query."""
        page = self._summaries.query(
            QueryRequest(
                partition_value=destination.tenant.arn_prefix,
                sort_condition=SortCondition(operator=SortOperator.BEGINS_WITH, value=f"{destination.destination_id}#"),
                max_results=1,
                consistency=ReadConsistency.STRONG,
            )
        )
        return bool(page.items)

    def update(self, update: ConnectorUpdate) -> ConnectorItem:
        """Write the members the merged item changed, only if the stored version is still the one read."""
        stored = update.stored.model_dump()
        merged = update.merged.model_dump()
        changed = frozenset(name for name, value in merged.items() if value is not None and value != stored[name])
        removed = sorted(name for name, value in merged.items() if value is None and stored[name] is not None)
        values = self._table.attributes(ItemAttributesRequest(item=update.merged, names=changed))
        sets = {f"m{index}": name for index, name in enumerate(sorted(changed))}
        removals = {f"r{index}": name for index, name in enumerate(removed)}
        expression = ", ".join([*(f"#{key} = :{key}" for key in sets), "#version = :next", "updated_at = :now"])
        remove = "" if not removals else f" REMOVE {', '.join(f'#{key}' for key in removals)}"
        return self._table.update(
            UpdateItemRequest(
                key=_key(update.stored.ref),
                action="Custom connector update",
                expression=f"SET {expression}{remove}",
                condition="#version = :version AND #status = :available",
                names={**{f"#{key}": name for key, name in {**sets, **removals}.items()}, "#version": "version"}
                | _STATUS,
                values={
                    **{f":{key}": values[name] for key, name in sets.items()},
                    ":version": update.stored.version,
                    ":next": update.stored.version + 1,
                    ":now": self._clock.now(),
                    **_AVAILABLE,
                },
            )
        )

    def begin_deletion(self, connector: ConnectorRef) -> ConnectorItem:
        """Set an `AVAILABLE` custom connector to `DELETING`, which blocks new jobs and writes."""
        return self._table.update(
            UpdateItemRequest(
                key=_key(connector),
                action="Custom connector deletion start",
                expression="SET #status = :deleting, updated_at = :now ADD #version :one",
                condition="#status = :available",
                names={**_STATUS, "#version": "version"},
                values={
                    ":deleting": ConnectorStatus.DELETING.value,
                    ":now": self._clock.now(),
                    ":one": 1,
                    **_AVAILABLE,
                },
            )
        )

    def record_deletion_execution(self, record: DeletionExecutionRecord) -> None:
        """Store the ARN of the deletion execution most recently started for a `DELETING` custom connector."""
        self._table.update(
            UpdateItemRequest(
                key=_key(record.connector),
                action="Deletion execution record",
                expression="SET deletion_execution_arn = :arn, updated_at = :now",
                condition="#status = :deleting",
                names=_STATUS,
                values={
                    ":arn": record.execution_arn,
                    ":deleting": ConnectorStatus.DELETING.value,
                    ":now": self._clock.now(),
                },
            )
        )

    def new_item(self, record: CreateConnectorRecord) -> ConnectorItem:
        """The item `create` saves for the record, before it is saved."""
        connector = record.connector
        payload = record.payload
        now = float(self._clock.now())
        return ConnectorItem(
            tenant_arn_prefix=connector.tenant.arn_prefix,
            destination_connector_key=connector.destination_connector_key,
            destination_name_key=f"{connector.destination_id}#{payload.name}",
            connector_id=connector.connector_id,
            destination_id=connector.destination_id,
            arn=connector.arn,
            name=payload.name,
            description=payload.description,
            status=ConnectorStatus.AVAILABLE,
            runtime_role_arn=record.runtime_role_arn,
            container_properties=ContainerProperties.model_validate(payload.container_properties.model_dump()),
            source_access=SourceAccess(
                role_arn=payload.source_access.role_arn,
                secret_arn=payload.source_access.secret_arn,
                external_id=record.external_id,
            ),
            log_destination=payload.log_destination,
            schedule=payload.schedule,
            encrypted_configuration=record.encrypted_configuration,
            job_definition_arn=record.job_definition_arn,
            created_at=now,
            updated_at=now,
            version=1,
        )


def _key(connector: ConnectorRef) -> ItemKey:
    return ItemKey(partition=connector.tenant.arn_prefix, sort=connector.destination_connector_key)
