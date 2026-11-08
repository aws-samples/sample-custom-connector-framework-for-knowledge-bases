"""The DAOs every composition root shares, built once per cold start; the roots that run jobs add the jobs DAO."""

from __future__ import annotations

import boto3
from ccf_api_models.base import ValueModel
from pydantic import ConfigDict

from common.resource_data_codec import ResourceDataCodec
from common.resource_ids import SystemClock
from common.settings import JobSettings, TableNames
from common.storage.ddb.collections_dao import CollectionsDao, CollectionsTable
from common.storage.ddb.custom_connector_jobs_dao import CustomConnectorJobsDao, JobsTables
from common.storage.ddb.custom_connectors_dao import CustomConnectorsDao, CustomConnectorsTables
from common.storage.ddb.destinations_dao import DestinationsDao, DestinationsTable
from common.storage.ddb.documents_dao import DocumentsDao, DocumentsTable


class DataStoresRequest(ValueModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    tables: TableNames
    codec: ResourceDataCodec
    """`CollectionsDao` encrypts and decrypts checkpoints with it."""


class DataStores(ValueModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    destinations: DestinationsDao
    connectors: CustomConnectorsDao
    collections: CollectionsDao
    documents: DocumentsDao

    @classmethod
    def build(cls, request: DataStoresRequest) -> DataStores:
        """One DynamoDB resource and every DAO over it; no network call happens here."""
        tables = request.tables
        dynamodb = boto3.resource("dynamodb")
        connectors = CustomConnectorsDao.over(
            CustomConnectorsTables(
                table=dynamodb.Table(tables.connectors),
                destinations_table=dynamodb.Table(tables.destinations),
                client=dynamodb.meta.client,
                clock=SystemClock(),
            )
        )
        documents = DocumentsDao.over(
            DocumentsTable(table=dynamodb.Table(tables.documents), client=dynamodb.meta.client, clock=SystemClock())
        )
        return cls(
            destinations=DestinationsDao.over(
                DestinationsTable(
                    table=dynamodb.Table(tables.destinations), client=dynamodb.meta.client, clock=SystemClock()
                )
            ),
            connectors=connectors,
            collections=CollectionsDao.over(
                CollectionsTable(
                    table=dynamodb.Table(tables.collections),
                    connectors_table=dynamodb.Table(tables.connectors),
                    client=dynamodb.meta.client,
                    codec=request.codec,
                    clock=SystemClock(),
                )
            ),
            documents=documents,
        )


class JobsDaoRequest(ValueModel):
    """The roots that start or stop jobs: the table names, and the job settings the records' expiry comes from."""

    tables: TableNames
    jobs: JobSettings


def build_jobs_dao(request: JobsDaoRequest) -> CustomConnectorJobsDao:
    """The jobs DAO over its own DynamoDB resource; no network call happens here."""
    tables = request.tables
    dynamodb = boto3.resource("dynamodb")
    return CustomConnectorJobsDao.over(
        JobsTables(
            jobs_table=dynamodb.Table(tables.jobs),
            connectors_table=dynamodb.Table(tables.connectors),
            destinations_table=dynamodb.Table(tables.destinations),
            client=dynamodb.meta.client,
            settings=request.jobs,
            clock=SystemClock(),
        )
    )
