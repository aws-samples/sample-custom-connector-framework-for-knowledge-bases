"""Every DAO over one set of moto tables, the identifiers the tests use, and builders for starting records."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ccf_api_models.identifiers import CollectionRef, ConnectorRef, DestinationRef, JobRef, Partition, TenantContext
from ccf_api_models.models import (
    ContainerProperties,
    CreateCustomConnectorRequest,
    CreateDestinationRequest,
    DestinationType,
    LogDestination,
    ResourceRequirements,
    SourceAccessInput,
)
from pydantic import BaseModel, ConfigDict, Field

from common.resource_data_codec import ResourceDataCodec
from common.resource_ids import SystemClock
from common.settings import JobSettings, TableNames
from common.storage.ddb.collections_dao import CollectionsDao, CollectionsTable
from common.storage.ddb.custom_connector_jobs_dao import CustomConnectorJobsDao, JobsTables, StartJobRecord
from common.storage.ddb.custom_connectors_dao import CreateConnectorRecord, CustomConnectorsDao, CustomConnectorsTables
from common.storage.ddb.destinations_dao import CreateDestinationRecord, DestinationsDao, DestinationsTable
from common.storage.ddb.documents_dao import DocumentsDao, DocumentsTable

TENANT_ACCOUNT_ID = "111122223333"
JOB_SETTINGS = JobSettings(
    job_execution_arn="arn:aws:states:us-east-1:444455556666:stateMachine:CcfJobExecution",
    record_retention_seconds=7 * 24 * 60 * 60,
    active_record_retention_seconds=14 * 24 * 60 * 60,
)


class DaoTables(BaseModel):
    """The moto resource and table names a world reads, the codec, and any tables a test has wrapped."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    resource: Any
    names: TableNames
    codec: ResourceDataCodec
    replaced_tables: Mapping[str, Any] = Field(default_factory=dict)

    def table(self, name: str) -> Any:
        return self.replaced_tables.get(name) or self.resource.Table(name)


class DaoWorld:
    """Every DAO over one set of moto tables, the identifiers the tests use, and builders for starting records."""

    class DestinationSeed(BaseModel):
        """A destination to create; the world's own destination unless another is named."""

        model_config = ConfigDict(frozen=True)

        destination: DestinationRef | None = None
        name: str = "docs"
        access_control_enabled: bool = False

    class ConnectorSeed(BaseModel):
        """A custom connector to create; the world's own custom connector unless another is named."""

        model_config = ConfigDict(frozen=True)

        connector: ConnectorRef | None = None
        name: str = "github-awslabs"
        log_destination: LogDestination | None = None

    class StatusChange(BaseModel):
        """A destination status to write directly, as the destination workflow does."""

        model_config = ConfigDict(frozen=True)

        status: str
        destination: DestinationRef | None = None

    tenant = TenantContext(partition=Partition.AWS, region="us-east-1", account_id=TENANT_ACCOUNT_ID)
    other_tenant = TenantContext(partition=Partition.AWS, region="us-east-1", account_id="444455556666")
    destination = DestinationRef(tenant=tenant, destination_id="dst-7c41e9a2b5d0")
    other_destination = DestinationRef(tenant=tenant, destination_id="dst-0a1b2c3d4e5f")
    connector = ConnectorRef(destination=destination, connector_id="cc-e020d65d76c0")
    job = JobRef(connector=connector, job_id="ccj-0fdef5d64630")
    destination_role_arn = f"arn:aws:iam::{TENANT_ACCOUNT_ID}:role/ccf/destinations/CcfDestinationRole-docs"
    runtime_role_arn = "arn:aws:iam::444455556666:role/ccf-runtime/CcfConnectorRuntime-3c9e1f0a2b7d4e6f8a1b2c3d4e5f6a7b"
    source_role_arn = f"arn:aws:iam::{TENANT_ACCOUNT_ID}:role/CcfSourceRole-github"
    log_group_arn = f"arn:aws:logs:us-east-1:{TENANT_ACCOUNT_ID}:log-group:/ccf/connectors/github"
    log_role_arn = f"arn:aws:iam::{TENANT_ACCOUNT_ID}:role/CcfLogDeliveryRole-github"

    def __init__(self, tables: DaoTables) -> None:
        names = tables.names
        self.resource = tables.resource
        self.names = names
        self._tables = tables
        self.destinations = DestinationsDao.over(
            DestinationsTable(
                table=tables.table(names.destinations), client=tables.resource.meta.client, clock=SystemClock()
            )
        )
        self.connectors = CustomConnectorsDao.over(
            CustomConnectorsTables(
                table=tables.table(names.connectors),
                destinations_table=tables.table(names.destinations),
                client=tables.resource.meta.client,
                clock=SystemClock(),
            )
        )
        self.jobs = CustomConnectorJobsDao.over(
            JobsTables(
                jobs_table=tables.table(names.jobs),
                connectors_table=tables.table(names.connectors),
                destinations_table=tables.table(names.destinations),
                client=tables.resource.meta.client,
                settings=JOB_SETTINGS,
                clock=SystemClock(),
            )
        )
        self.documents = DocumentsDao.over(
            DocumentsTable(table=tables.table(names.documents), client=tables.resource.meta.client, clock=SystemClock())
        )
        self.collections = CollectionsDao.over(
            CollectionsTable(
                table=tables.table(names.collections),
                connectors_table=tables.table(names.connectors),
                client=tables.resource.meta.client,
                codec=tables.codec,
                clock=SystemClock(),
            )
        )

    def spying_on(self, name: str) -> tuple[DaoWorld, ConsistentReadSpy]:
        """Return this world with the named table's reads recorded, and the spy that records them."""
        spy = ConsistentReadSpy(self.table(name))
        replaced = {**self._tables.replaced_tables, name: spy}
        return DaoWorld(self._tables.model_copy(update={"replaced_tables": replaced})), spy

    def table(self, name: str) -> Any:
        return self.resource.Table(name)

    def destination_record(self, seed: DestinationSeed | None = None) -> CreateDestinationRecord:
        spec = seed or self.DestinationSeed()
        payload = CreateDestinationRequest(
            name=spec.name,
            type=DestinationType.BEDROCK,
            role_arn=self.destination_role_arn,
            access_control_enabled=spec.access_control_enabled,
        )
        return CreateDestinationRecord(
            destination=spec.destination or self.destination, payload=payload, correlation_id="correlation-1"
        )

    def connector_record(self, seed: ConnectorSeed | None = None) -> CreateConnectorRecord:
        spec = seed or self.ConnectorSeed()
        target = spec.connector or self.connector
        payload = CreateCustomConnectorRequest(
            destination_id=target.destination_id,
            name=spec.name,
            container_properties=ContainerProperties(
                image_uri=f"{TENANT_ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/ccf-connectors/github:1.0.0",
                command=["ccf-connector", "run", "connector:GitHubConnector"],
                resource_requirements=ResourceRequirements(cpu=1.0, memory=2048),
                timeout=3600,
            ),
            source_access=SourceAccessInput(role_arn=self.source_role_arn),
            log_destination=spec.log_destination,
        )
        return CreateConnectorRecord(
            connector=target,
            payload=payload,
            external_id="ccf-ext-0123456789abcdef0123456789abcdef",
            runtime_role_arn=self.runtime_role_arn,
            encrypted_configuration=b"ciphertext",
        )

    def set_destination_status(self, change: StatusChange) -> None:
        target = change.destination or self.destination
        self.table(self.names.destinations).update_item(
            Key={"tenant_arn_prefix": target.tenant.arn_prefix, "destination_id": target.destination_id},
            UpdateExpression="SET #status = :status",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":status": change.status},
        )

    def set_job_status(self, status: str) -> None:
        """Write the world's job's status directly, as the job state machine does."""
        self.table(self.names.jobs).update_item(
            Key={"tenant_arn_prefix": self.tenant.arn_prefix, "job_id": self.job.job_id},
            UpdateExpression="SET #status = :status",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":status": status},
        )

    def collection(self, collection_id: str = "col-3f9a1c2b5d7e9f01") -> CollectionRef:
        return CollectionRef(connector=self.connector, collection_id=collection_id)

    def active_destination(self, destination: DestinationRef | None = None) -> None:
        self.destinations.create(self.destination_record(self.DestinationSeed(destination=destination)))
        self.set_destination_status(self.StatusChange(status="ACTIVE", destination=destination))

    def available_connector(self, connector: ConnectorRef | None = None) -> None:
        target = connector or self.connector
        self.active_destination(target.destination)
        self.connectors.create(self.connector_record(self.ConnectorSeed(connector=target)))

    def started_job(self) -> None:
        self.available_connector()
        self.jobs.start_job(StartJobRecord(job=self.job, encrypted_configuration=b"job-ciphertext"))


class ConsistentReadSpy:
    """Wraps a table and records whether each `get_item` asked for a strongly consistent read."""

    def __init__(self, table: Any) -> None:
        self._table = table
        self.reads: list[bool] = []

    def get_item(self, **arguments: Any) -> Any:
        self.reads.append(arguments.get("ConsistentRead") is True)
        return self._table.get_item(**arguments)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._table, name)
