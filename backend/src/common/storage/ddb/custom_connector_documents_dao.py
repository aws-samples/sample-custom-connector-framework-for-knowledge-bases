"""Tenant-scoped DynamoDB lifecycle for connector document state."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from time import time
from typing import TYPE_CHECKING, Any

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from ccf_api_models.models import DocumentItem, DocumentSummary

from common.storage.ddb.custom_connectors_dao import CustomConnectorsDao, GetConnectorRequest
from common.storage.errors import StorageNotFoundError, StorageOperationError
from common.tenant import TenantContext

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.service_resource import Table


@dataclass(frozen=True)
class BatchPutDocumentsRequest:
    """Tenant-scoped document state upsert."""

    tenant_context: TenantContext
    connector_id: str
    documents: list[DocumentItem]


@dataclass(frozen=True)
class BatchDeleteDocumentsRequest:
    """Tenant-scoped document state deletion."""

    tenant_context: TenantContext
    connector_id: str
    document_ids: list[str]


@dataclass(frozen=True)
class ListDocumentsRequest:
    """Tenant-scoped document state listing."""

    tenant_context: TenantContext
    connector_id: str
    max_results: int = 50
    next_token: str | None = None


@dataclass(frozen=True)
class ListDocumentsResponse:
    """Generated document summaries and opaque pagination state."""

    documents: list[DocumentSummary]
    next_token: str | None = None


class CustomConnectorDocumentsDao:
    """Manages document state for one connector table."""

    def __init__(self, documents_table: Table, connectors_dao: CustomConnectorsDao) -> None:
        self.table = documents_table
        self.connectors_dao = connectors_dao

    def batch_put_documents(self, request: BatchPutDocumentsRequest) -> None:
        """Upsert document state without document content or ACL identities."""
        self._verify_connector(request.tenant_context, request.connector_id)
        now = Decimal(str(time()))
        try:
            with self.table.batch_writer() as batch:
                for document in request.documents:
                    batch.put_item(
                        Item={
                            "custom_connector_arn_prefix": request.tenant_context.get_arn_prefix(),
                            "connector_document_key": self._document_key(
                                request.connector_id,
                                document.document_id,
                            ),
                            "document_id": document.document_id,
                            "connector_id": request.connector_id,
                            "checksum": document.checksum,
                            "object_key": document.object_key,
                            "sidecar_key": document.sidecar_key,
                            "created_at": now,
                            "updated_at": now,
                        }
                    )
        except ClientError as error:
            raise StorageOperationError("Document state write failed") from error

    def batch_delete_documents(self, request: BatchDeleteDocumentsRequest) -> None:
        """Delete document state owned by a connector."""
        self._verify_connector(request.tenant_context, request.connector_id)
        try:
            with self.table.batch_writer() as batch:
                for document_id in request.document_ids:
                    batch.delete_item(
                        Key=self._key(
                            request.tenant_context,
                            request.connector_id,
                            document_id,
                        )
                    )
        except ClientError as error:
            raise StorageOperationError("Document state deletion failed") from error

    def list_documents(self, request: ListDocumentsRequest) -> ListDocumentsResponse:
        """List document state for exactly one tenant and connector."""
        self._verify_connector(request.tenant_context, request.connector_id)
        query: dict[str, Any] = {
            "IndexName": "GSI1",
            "KeyConditionExpression": Key("custom_connector_arn_prefix").eq(request.tenant_context.get_arn_prefix())
            & Key("connector_id").eq(request.connector_id),
            "Limit": request.max_results,
        }
        if request.next_token is not None:
            query["ExclusiveStartKey"] = self._decode_token(request.next_token)
        try:
            response = self.table.query(**query)
        except ClientError as error:
            raise StorageOperationError("Document state listing failed") from error
        return ListDocumentsResponse(
            documents=[self._summary(item) for item in response.get("Items", [])],
            next_token=self._encode_token(response.get("LastEvaluatedKey")),
        )

    @staticmethod
    def _summary(item: Mapping[str, object]) -> DocumentSummary:
        sidecar_key = item.get("sidecar_key")
        created_at = item.get("created_at")
        updated_at = item.get("updated_at")
        if sidecar_key is not None and not isinstance(sidecar_key, str):
            raise StorageOperationError("Stored document sidecar key is invalid")
        if not isinstance(created_at, (str, int, float, Decimal)) or not isinstance(
            updated_at, (str, int, float, Decimal)
        ):
            raise StorageOperationError("Stored document timestamp is invalid")
        return DocumentSummary(
            document_id=str(item["document_id"]),
            checksum=str(item["checksum"]),
            object_key=str(item["object_key"]),
            sidecar_key=sidecar_key,
            created_at=float(created_at),
            updated_at=float(updated_at),
        )

    def _verify_connector(self, tenant: TenantContext, connector_id: str) -> None:
        try:
            self.connectors_dao.get_connector(GetConnectorRequest(tenant, connector_id))
        except StorageNotFoundError:
            raise StorageNotFoundError("Connector does not exist") from None

    @staticmethod
    def _key(tenant: TenantContext, connector_id: str, document_id: str) -> dict[str, str]:
        return {
            "custom_connector_arn_prefix": tenant.get_arn_prefix(),
            "connector_document_key": CustomConnectorDocumentsDao._document_key(connector_id, document_id),
        }

    @staticmethod
    def _document_key(connector_id: str, document_id: str) -> str:
        return f"{connector_id}#{document_id}"

    @staticmethod
    def _decode_token(token: str) -> dict[str, Any]:
        try:
            decoded: Any = json.loads(token)
        except json.JSONDecodeError as error:
            raise StorageOperationError("Pagination token is invalid") from error
        if not isinstance(decoded, dict):
            raise StorageOperationError("Pagination token is invalid")
        return decoded

    @staticmethod
    def _encode_token(key: Any) -> str | None:
        return json.dumps(key) if isinstance(key, dict) else None


DaoResourceNotFoundError = StorageNotFoundError
DaoInternalError = StorageOperationError
DaoConflictError = StorageOperationError
