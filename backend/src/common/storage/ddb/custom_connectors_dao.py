"""Tenant-scoped DynamoDB lifecycle for custom connectors and checkpoints."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from time import time
from typing import TYPE_CHECKING, Any

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from ccf_api_models.models import (
    Checkpoint,
    ConnectorSchedule,
    ConnectorStatus,
    ContainerProperties,
    CreateCustomConnectorRequest,
    CustomConnectorSummary,
    ScheduleState,
    SourceAccessConfiguration,
    UpdateCustomConnectorRequest,
)
from pydantic import SecretStr

from common.storage.errors import StorageConflictError, StorageNotFoundError, StorageOperationError
from common.tenant import TenantContext

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.service_resource import Table


@dataclass(frozen=True)
class CreateConnectorRequest:
    """Generated create input plus service-owned identity and ciphertext."""

    tenant: TenantContext
    connector_id: str
    payload: CreateCustomConnectorRequest
    encrypted_configuration: bytes
    runtime_role_arn: str


@dataclass(frozen=True)
class UpdateConnectorRequest:
    """Generated update input and replacement ciphertext when supplied."""

    tenant: TenantContext
    payload: UpdateCustomConnectorRequest
    encrypted_configuration: bytes | None


@dataclass(frozen=True)
class GetConnectorRequest:
    """Tenant-scoped connector lookup."""

    tenant_context: TenantContext
    connector_id: str


@dataclass(frozen=True)
class ListConnectorsRequest:
    """Tenant-scoped connector listing."""

    tenant_context: TenantContext
    max_results: int = 50
    next_token: str | None = None


@dataclass(frozen=True)
class ListConnectorsResponse:
    """Generated connector summaries and opaque pagination state."""

    connectors: list[CustomConnectorSummary]
    next_token: str | None = None


@dataclass(frozen=True)
class DeleteConnectorRequest:
    """Tenant-scoped connector deletion."""

    tenant_context: TenantContext
    connector_id: str


@dataclass(frozen=True)
class ClaimConnectorDeletionRequest:
    """Tenant-scoped deletion claim taken before any external revocation."""

    tenant_context: TenantContext
    connector_id: str


@dataclass(frozen=True)
class UpdateConnectorStatusRequest:
    """Service-owned connector reservation transition bound to one job."""

    tenant_context: TenantContext
    connector_id: str
    status: ConnectorStatus
    job_id: str


@dataclass(frozen=True)
class PutCheckpointRequest:
    """Tenant-scoped checkpoint replacement."""

    tenant_context: TenantContext
    connector_id: str
    checkpoint_data: str


@dataclass(frozen=True)
class GetCheckpointRequest:
    """Tenant-scoped checkpoint lookup."""

    tenant_context: TenantContext
    connector_id: str


@dataclass(frozen=True)
class DeleteCheckpointRequest:
    """Tenant-scoped checkpoint deletion."""

    tenant_context: TenantContext
    connector_id: str


@dataclass(frozen=True)
class StoredConnector:
    """Connector record whose configuration remains encrypted."""

    connector_id: str
    arn: str
    name: str
    created_at: float
    updated_at: float
    status: ConnectorStatus
    container_properties: ContainerProperties
    encrypted_configuration: bytes
    source_access: SourceAccessConfiguration
    description: str | None = None
    schedule: ConnectorSchedule | None = None
    runtime_role_arn: str | None = None
    version: int = 1


class CustomConnectorsDao:
    """Manages the connector and checkpoint lifecycle in one table."""

    def __init__(self, table: Table) -> None:
        self.table = table

    def create_connector(self, request: CreateConnectorRequest) -> CustomConnectorSummary:
        """Persist one connector containing ciphertext only."""
        now = self._now()
        arn = request.tenant.get_connector_arn(request.connector_id)
        item = {
            "custom_connector_arn_prefix": request.tenant.get_arn_prefix(),
            "connector_id": request.connector_id,
            "arn": arn,
            "name": request.payload.name,
            "description": request.payload.description,
            "container_properties": self._to_dynamodb(request.payload.container_properties.model_dump(mode="json")),
            "encrypted_configuration": request.encrypted_configuration,
            "source_access": request.payload.source_access.model_dump(mode="json"),
            "schedule": request.payload.schedule.model_dump(mode="json") if request.payload.schedule else None,
            "runtime_role_arn": request.runtime_role_arn,
            "status": ConnectorStatus.AVAILABLE.value,
            "created_at": Decimal(str(now)),
            "updated_at": Decimal(str(now)),
            "version": 1,
        }
        try:
            self.table.put_item(Item=item, ConditionExpression="attribute_not_exists(connector_id)")
        except ClientError as error:
            if self._error_code(error) == "ConditionalCheckFailedException":
                raise StorageConflictError("Connector identity already exists") from error
            raise StorageOperationError("Connector creation failed") from error
        return self._summary(item)

    def get_connector(self, request: GetConnectorRequest) -> StoredConnector:
        """Fetch a tenant-scoped connector without decrypting configuration."""
        item = self._get_item(request.tenant_context, request.connector_id)
        return self._stored_connector(item)

    def list_connectors(self, request: ListConnectorsRequest) -> ListConnectorsResponse:
        """List connector summaries without configuration ciphertext."""
        query: dict[str, Any] = {
            "KeyConditionExpression": Key("custom_connector_arn_prefix").eq(request.tenant_context.get_arn_prefix()),
            "Limit": request.max_results,
        }
        if request.next_token is not None:
            query["ExclusiveStartKey"] = self._decode_token(request.next_token)
        try:
            response = self.table.query(**query)
        except ClientError as error:
            raise StorageOperationError("Connector listing failed") from error
        items = response.get("Items", [])
        return ListConnectorsResponse(
            connectors=[self._summary(item) for item in items],
            next_token=self._encode_token(response.get("LastEvaluatedKey")),
        )

    def claim_connector_deletion(self, request: ClaimConnectorDeletionRequest) -> None:
        """Claim an idle connector for deletion so no job can reserve it.

        Taking this marker before revoking the runtime role closes the window
        between an availability check and the record delete, in which
        StartCustomConnectorJob could otherwise reserve the connector.
        """
        try:
            self.table.update_item(
                Key=self._key(request.tenant_context, request.connector_id),
                UpdateExpression="SET deletion_claimed_at = :claimed_at, updated_at = :claimed_at ADD #version :one",
                ConditionExpression=(
                    "attribute_exists(connector_id) AND #status = :available "
                    "AND attribute_not_exists(active_job_id) AND attribute_not_exists(deletion_claimed_at)"
                ),
                ExpressionAttributeNames={"#status": "status", "#version": "version"},
                ExpressionAttributeValues={
                    ":available": ConnectorStatus.AVAILABLE.value,
                    ":claimed_at": Decimal(str(self._now())),
                    ":one": 1,
                },
            )
        except ClientError as error:
            if self._error_code(error) != "ConditionalCheckFailedException":
                raise StorageOperationError("Connector deletion claim failed") from error
            self._get_item(request.tenant_context, request.connector_id)
            raise StorageConflictError("Connector is in use") from error

    def release_connector_deletion(self, request: ClaimConnectorDeletionRequest) -> None:
        """Release a deletion claim after a failed deletion, if the record remains."""
        try:
            self.table.update_item(
                Key=self._key(request.tenant_context, request.connector_id),
                UpdateExpression="SET updated_at = :updated_at REMOVE deletion_claimed_at ADD #version :one",
                ConditionExpression="attribute_exists(connector_id)",
                ExpressionAttributeNames={"#version": "version"},
                ExpressionAttributeValues={":updated_at": Decimal(str(self._now())), ":one": 1},
            )
        except ClientError as error:
            if self._error_code(error) != "ConditionalCheckFailedException":
                raise StorageOperationError("Connector deletion claim release failed") from error

    def delete_connector(self, request: DeleteConnectorRequest) -> None:
        """Delete a connector that is claimed for deletion and still idle."""
        key = self._key(request.tenant_context, request.connector_id)
        try:
            self.table.delete_item(
                Key=key,
                ConditionExpression=(
                    "attribute_exists(connector_id) AND #status <> :in_use "
                    "AND attribute_not_exists(active_job_id) AND attribute_exists(deletion_claimed_at)"
                ),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":in_use": ConnectorStatus.IN_USE.value},
            )
        except ClientError as error:
            if self._error_code(error) != "ConditionalCheckFailedException":
                raise StorageOperationError("Connector deletion failed") from error
            try:
                self._get_item(request.tenant_context, request.connector_id)
            except StorageNotFoundError:
                raise
            raise StorageConflictError("Connector is in use") from error

    def update_connector(self, request: UpdateConnectorRequest) -> CustomConnectorSummary:
        """Replace supplied values while retaining no configuration history."""
        connector_id = request.payload.connector_id
        item = self._get_item(request.tenant, connector_id)
        in_use = item["status"] == ConnectorStatus.IN_USE.value
        if request.encrypted_configuration is not None and in_use:
            raise StorageConflictError("Connector configuration cannot change while the connector is in use")
        # A running task already holds credentials scoped to the registered
        # source role, so re-pointing source access mid-run would change the
        # permissions of a live execution.
        if request.payload.source_access is not None and in_use:
            raise StorageConflictError("Connector source access cannot change while the connector is in use")
        self._apply_connector_update(item, request)
        current_version = int(item.get("version", 1))
        item["version"] = current_version + 1
        item["updated_at"] = Decimal(str(self._now()))
        try:
            self.table.put_item(
                Item=item,
                ConditionExpression="#version = :current_version",
                ExpressionAttributeNames={"#version": "version"},
                ExpressionAttributeValues={":current_version": current_version},
            )
        except ClientError as error:
            if self._error_code(error) == "ConditionalCheckFailedException":
                raise StorageConflictError("Connector changed during update") from error
            raise StorageOperationError("Connector update failed") from error
        return self._summary(item)

    def update_connector_status(self, request: UpdateConnectorStatusRequest) -> None:
        """Reserve or release a connector only for the requesting job."""
        if request.status is ConnectorStatus.IN_USE:
            update_expression = (
                "SET #status = :status, active_job_id = :job_id, updated_at = :updated_at ADD #version :one"
            )
            condition_expression = (
                "attribute_exists(connector_id) AND #status = :available "
                "AND attribute_not_exists(active_job_id) AND attribute_not_exists(deletion_claimed_at)"
            )
            values: dict[str, Any] = {
                ":status": ConnectorStatus.IN_USE.value,
                ":available": ConnectorStatus.AVAILABLE.value,
                ":job_id": request.job_id,
                ":updated_at": Decimal(str(self._now())),
                ":one": 1,
            }
        else:
            update_expression = "SET #status = :status, updated_at = :updated_at REMOVE active_job_id ADD #version :one"
            condition_expression = "attribute_exists(connector_id) AND #status = :in_use AND active_job_id = :job_id"
            values = {
                ":status": ConnectorStatus.AVAILABLE.value,
                ":in_use": ConnectorStatus.IN_USE.value,
                ":job_id": request.job_id,
                ":updated_at": Decimal(str(self._now())),
                ":one": 1,
            }
        try:
            self.table.update_item(
                Key=self._key(request.tenant_context, request.connector_id),
                UpdateExpression=update_expression,
                ConditionExpression=condition_expression,
                ExpressionAttributeNames={"#status": "status", "#version": "version"},
                ExpressionAttributeValues=values,
            )
        except ClientError as error:
            if self._error_code(error) != "ConditionalCheckFailedException":
                raise StorageOperationError("Connector status update failed") from error
            try:
                self._get_item(request.tenant_context, request.connector_id)
            except StorageNotFoundError:
                raise
            raise StorageConflictError("Connector reservation changed") from error

    def put_checkpoint(self, request: PutCheckpointRequest) -> None:
        """Replace a connector checkpoint."""
        item = self._get_item(request.tenant_context, request.connector_id)
        now = self._now()
        previous = item.get("checkpoint")
        created_at = previous.get("created_at", now) if isinstance(previous, dict) else now
        checkpoint = {
            "checkpoint_data": request.checkpoint_data,
            "created_at": Decimal(str(created_at)),
            "updated_at": Decimal(str(now)),
        }
        self._update_checkpoint(request.tenant_context, request.connector_id, item, checkpoint)

    def get_checkpoint(self, request: GetCheckpointRequest) -> Checkpoint:
        """Fetch a connector checkpoint."""
        item = self._get_item(request.tenant_context, request.connector_id)
        checkpoint = item.get("checkpoint")
        if not isinstance(checkpoint, dict):
            raise StorageNotFoundError("Checkpoint does not exist")
        return Checkpoint(
            connector_id=request.connector_id,
            checkpoint_data=SecretStr(str(checkpoint["checkpoint_data"])),
            created_at=float(checkpoint["created_at"]),
            updated_at=float(checkpoint["updated_at"]),
        )

    def delete_checkpoint(self, request: DeleteCheckpointRequest) -> None:
        """Delete an existing connector checkpoint."""
        item = self._get_item(request.tenant_context, request.connector_id)
        if "checkpoint" not in item:
            raise StorageNotFoundError("Checkpoint does not exist")
        current_version = int(item.get("version", 1))
        try:
            self.table.update_item(
                Key=self._key(request.tenant_context, request.connector_id),
                UpdateExpression="REMOVE checkpoint SET #version = :new_version, updated_at = :updated_at",
                ConditionExpression="#version = :current_version",
                ExpressionAttributeNames={"#version": "version"},
                ExpressionAttributeValues={
                    ":current_version": current_version,
                    ":new_version": current_version + 1,
                    ":updated_at": Decimal(str(self._now())),
                },
            )
        except ClientError as error:
            if self._error_code(error) == "ConditionalCheckFailedException":
                raise StorageConflictError("Connector changed during checkpoint deletion") from error
            raise StorageOperationError("Checkpoint deletion failed") from error

    def _get_item(self, tenant: TenantContext, connector_id: str) -> dict[str, Any]:
        try:
            response = self.table.get_item(Key=self._key(tenant, connector_id))
        except ClientError as error:
            raise StorageOperationError("Connector lookup failed") from error
        item = response.get("Item")
        if not isinstance(item, dict):
            raise StorageNotFoundError("Connector does not exist")
        return item

    def _apply_connector_update(self, item: dict[str, Any], request: UpdateConnectorRequest) -> None:
        payload = request.payload
        fields = payload.model_fields_set
        for field_name in ("name", "description", "source_access", "schedule"):
            if field_name not in fields:
                continue
            field_value = getattr(payload, field_name)
            item[field_name] = (
                field_value.model_dump(mode="json") if hasattr(field_value, "model_dump") else field_value
            )
        if payload.container_properties is not None:
            existing = dict(item["container_properties"])
            replacement = payload.container_properties.model_dump(mode="json", exclude_none=True)
            if "resource_requirements" in replacement:
                existing_requirements = dict(existing.get("resource_requirements", {}))
                existing_requirements.update(replacement["resource_requirements"])
                replacement["resource_requirements"] = existing_requirements
            existing.update(self._to_dynamodb(replacement))
            item["container_properties"] = existing
        if request.encrypted_configuration is not None:
            item["encrypted_configuration"] = request.encrypted_configuration

    def _update_checkpoint(
        self,
        tenant: TenantContext,
        connector_id: str,
        item: dict[str, Any],
        checkpoint: dict[str, Any],
    ) -> None:
        current_version = int(item.get("version", 1))
        try:
            self.table.update_item(
                Key=self._key(tenant, connector_id),
                UpdateExpression="SET checkpoint = :checkpoint, #version = :new_version, updated_at = :updated_at",
                ConditionExpression="#version = :current_version",
                ExpressionAttributeNames={"#version": "version"},
                ExpressionAttributeValues={
                    ":checkpoint": checkpoint,
                    ":current_version": current_version,
                    ":new_version": current_version + 1,
                    ":updated_at": checkpoint["updated_at"],
                },
            )
        except ClientError as error:
            if self._error_code(error) == "ConditionalCheckFailedException":
                raise StorageConflictError("Connector changed during checkpoint update") from error
            raise StorageOperationError("Checkpoint update failed") from error

    @staticmethod
    def _stored_connector(item: dict[str, Any]) -> StoredConnector:
        encrypted = item["encrypted_configuration"]
        ciphertext = encrypted.value if hasattr(encrypted, "value") else bytes(encrypted)
        return StoredConnector(
            connector_id=str(item["connector_id"]),
            arn=str(item["arn"]),
            name=str(item["name"]),
            created_at=float(item["created_at"]),
            updated_at=float(item["updated_at"]),
            status=ConnectorStatus(item["status"]),
            description=item.get("description"),
            container_properties=ContainerProperties.model_validate(
                CustomConnectorsDao._from_dynamodb(item["container_properties"])
            ),
            encrypted_configuration=ciphertext,
            source_access=SourceAccessConfiguration.model_validate(item["source_access"]),
            runtime_role_arn=(str(item["runtime_role_arn"]) if item.get("runtime_role_arn") else None),
            schedule=(
                ConnectorSchedule.model_validate(
                    {
                        **item["schedule"],
                        "state": ScheduleState(str(item["schedule"]["state"])),
                    }
                )
                if item.get("schedule")
                else None
            ),
            version=int(item.get("version", 1)),
        )

    @staticmethod
    def _summary(item: dict[str, Any]) -> CustomConnectorSummary:
        return CustomConnectorSummary(
            connector_id=str(item["connector_id"]),
            arn=str(item["arn"]),
            name=str(item["name"]),
            created_at=float(item["created_at"]),
            updated_at=float(item["updated_at"]),
            status=ConnectorStatus(item["status"]),
            description=item.get("description"),
        )

    @staticmethod
    def _key(tenant: TenantContext, connector_id: str) -> dict[str, str]:
        return {"custom_connector_arn_prefix": tenant.get_arn_prefix(), "connector_id": connector_id}

    @staticmethod
    def _to_dynamodb(value: Any) -> Any:
        if isinstance(value, float):
            return Decimal(str(value))
        if isinstance(value, dict):
            return {key: CustomConnectorsDao._to_dynamodb(nested) for key, nested in value.items()}
        if isinstance(value, list):
            return [CustomConnectorsDao._to_dynamodb(nested) for nested in value]
        return value

    @staticmethod
    def _from_dynamodb(value: Any) -> Any:
        if isinstance(value, Decimal):
            return int(value) if value == value.to_integral_value() else float(value)
        if isinstance(value, dict):
            return {key: CustomConnectorsDao._from_dynamodb(nested) for key, nested in value.items()}
        if isinstance(value, list):
            return [CustomConnectorsDao._from_dynamodb(nested) for nested in value]
        return value

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

    @staticmethod
    def _error_code(error: ClientError) -> str:
        return str(error.response.get("Error", {}).get("Code", ""))

    @staticmethod
    def _now() -> float:
        return time()


DaoConflictError = StorageConflictError
DaoResourceNotFoundError = StorageNotFoundError
DaoInternalError = StorageOperationError
