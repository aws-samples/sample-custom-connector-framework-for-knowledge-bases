"""Document-state isolation tests for the connector-scoped DynamoDB key."""

from unittest.mock import MagicMock

import boto3
from ccf_api_models.models import DocumentItem
from moto import mock_aws

from common.storage.ddb.custom_connector_documents_dao import (
    BatchDeleteDocumentsRequest,
    BatchPutDocumentsRequest,
    CustomConnectorDocumentsDao,
    ListDocumentsRequest,
)
from common.storage.ddb.custom_connectors_dao import CustomConnectorsDao
from common.tenant import TenantContext


def create_documents_dao() -> CustomConnectorDocumentsDao:
    client = boto3.client("dynamodb", region_name="us-east-1")
    client.create_table(
        TableName="documents",
        KeySchema=[
            {"AttributeName": "custom_connector_arn_prefix", "KeyType": "HASH"},
            {"AttributeName": "connector_document_key", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "custom_connector_arn_prefix", "AttributeType": "S"},
            {"AttributeName": "connector_document_key", "AttributeType": "S"},
            {"AttributeName": "connector_id", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "GSI1",
                "KeySchema": [
                    {"AttributeName": "custom_connector_arn_prefix", "KeyType": "HASH"},
                    {"AttributeName": "connector_id", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    table = boto3.resource("dynamodb", region_name="us-east-1").Table("documents")
    connectors_dao = MagicMock(spec=CustomConnectorsDao)
    connectors_dao.get_connector.return_value = None
    return CustomConnectorDocumentsDao(table, connectors_dao)


@mock_aws
def test_same_document_id_isolated_across_connectors_and_delete() -> None:
    documents = create_documents_dao()
    tenant = TenantContext("123456789012", "us-east-1")
    shared_id = "document-1"

    for connector_id, checksum in (("connector-a", "checksum-a"), ("connector-b", "checksum-b")):
        documents.batch_put_documents(
            BatchPutDocumentsRequest(
                tenant,
                connector_id,
                [DocumentItem(document_id=shared_id, checksum=checksum, object_key=f"{connector_id}/doc.txt")],
            )
        )

    documents.batch_delete_documents(BatchDeleteDocumentsRequest(tenant, "connector-a", [shared_id]))

    assert documents.list_documents(ListDocumentsRequest(tenant, "connector-a")).documents == []
    remaining = documents.list_documents(ListDocumentsRequest(tenant, "connector-b")).documents
    assert [(document.document_id, document.checksum) for document in remaining] == [(shared_id, "checksum-b")]
