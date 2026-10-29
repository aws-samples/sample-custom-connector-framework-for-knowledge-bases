"""One activity per custom-connector control-plane operation."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from ccf_api_models.models import (
    CcfServiceReference,
    ConnectorSchedule,
    CreateCustomConnectorRequest,
    CreateCustomConnectorResponseContent,
    CustomConnectorDetail,
    DeleteCustomConnectorRequest,
    DeploymentDestination,
    DestinationMode,
    GetCustomConnectorRequest,
    GetCustomConnectorResponseContent,
    ListCustomConnectorsRequest,
    ListCustomConnectorsResponseContent,
    SourceAccessConfiguration,
    UpdateCustomConnectorRequest,
    UpdateCustomConnectorResponseContent,
)

from common.configuration import (
    ConfigurationCodec,
    ConfigurationValidationError,
    ConfigurationValidator,
    DecryptConfigurationRequest,
    EncryptConfigurationRequest,
)
from common.operation import OperationRequest
from common.runtime_role import (
    ConnectorRuntimeRoleLifecycle,
    RuntimeRoleError,
    RuntimeRoleIdentity,
    RuntimeRolePolicyRequest,
    create_object_prefix,
)
from common.schedule_lifecycle import (
    ScheduleIdentity,
    ScheduleLifecycle,
    ScheduleLifecycleRequest,
)
from common.storage.ddb.custom_connectors_dao import (
    ClaimConnectorDeletionRequest,
    CreateConnectorRequest,
    CustomConnectorsDao,
    DeleteConnectorRequest,
    GetConnectorRequest,
    ListConnectorsRequest,
    StoredConnector,
    UpdateConnectorRequest,
)
from common.tenant import RoleIdentity, TenantContext, TenantContextError

_ECR_IMAGE_ACCOUNT = re.compile(r"^([0-9]{12})\.dkr\.ecr\.")
# The shared connector-runtime permissions boundary caps sts:AssumeRole to
# CcfSourceRole-* in configured client accounts, so registration must reject
# any source role the boundary would deny rather than accept it and fail at
# run time. See SOURCE_ROLE_SHAPE_LIMITATION in the task report.
SOURCE_ROLE_NAME_PREFIX = "CcfSourceRole-"


def _require_rest_api_id(rest_api_id: str | None) -> str:
    """Return the API Gateway-authored REST API id or fail closed."""
    if rest_api_id is None:
        raise RuntimeRoleError("The serving REST API id is unavailable")
    return rest_api_id


@dataclass(frozen=True)
class ServiceReferenceConfiguration:
    """Deployment-owned values exposed through the generated service reference."""

    account_id: str
    endpoint: str
    region: str
    partition: str
    service_principal: str
    ingestion_bucket_name: str
    destination_mode: DestinationMode


@dataclass(frozen=True)
class VerifyConnectorOwnershipRequest:
    """Authenticated tenant plus the client-supplied identities to verify."""

    tenant: TenantContext
    image_uri: str | None
    source_access: SourceAccessConfiguration | None


class ConnectorOwnershipValidator:
    """Rejects identities outside the caller's account or outside the boundary."""

    def __init__(self, source_role_account_ids: frozenset[str]) -> None:
        self._source_role_account_ids = source_role_account_ids

    def verify(self, request: VerifyConnectorOwnershipRequest) -> None:
        """Verify every supplied client identity belongs to the authenticated tenant."""
        tenant_account_id = request.tenant.account_id
        if request.image_uri is not None:
            match = _ECR_IMAGE_ACCOUNT.match(request.image_uri)
            if match is None or match.group(1) != tenant_account_id:
                raise ConfigurationValidationError("Connector image repository must belong to the calling account")
        if request.source_access is None:
            return
        try:
            source_role = RoleIdentity.from_role_arn(request.source_access.role_arn)
        except TenantContextError as error:
            raise ConfigurationValidationError("Connector source role ARN is malformed") from error
        if source_role.account_id != tenant_account_id:
            raise ConfigurationValidationError("Connector source role must belong to the calling account")
        if tenant_account_id not in self._source_role_account_ids:
            raise ConfigurationValidationError("Connector source role account is not a configured client account")
        if not source_role.role_name.startswith(SOURCE_ROLE_NAME_PREFIX):
            raise ConfigurationValidationError(f"Connector source role name must start with {SOURCE_ROLE_NAME_PREFIX}")
        # The boundary names role/CcfSourceRole-*, which an IAM path defeats.
        if not request.source_access.role_arn.endswith(f":role/{source_role.role_name}"):
            raise ConfigurationValidationError("Connector source role must not use an IAM path")


class CreateCustomConnectorActivity:
    """Verify ownership, encrypt, provision a runtime role, and create one connector."""

    def __init__(
        self,
        dao: CustomConnectorsDao,
        validator: ConfigurationValidator,
        codec: ConfigurationCodec,
        schedule_lifecycle: ScheduleLifecycle,
        ownership_validator: ConnectorOwnershipValidator,
        runtime_roles: ConnectorRuntimeRoleLifecycle,
    ) -> None:
        self._dao = dao
        self._validator = validator
        self._codec = codec
        self._schedule_lifecycle = schedule_lifecycle
        self._ownership_validator = ownership_validator
        self._runtime_roles = runtime_roles

    def create(
        self,
        request: OperationRequest[CreateCustomConnectorRequest],
    ) -> CreateCustomConnectorResponseContent:
        """Create a connector with ciphertext-only persistence and a scoped runtime role."""
        payload = request.payload
        self._ownership_validator.verify(
            VerifyConnectorOwnershipRequest(
                tenant=request.tenant,
                image_uri=payload.container_properties.image_uri,
                source_access=payload.source_access,
            )
        )
        connector_id = f"cc-{uuid.uuid4().hex[:12]}"
        connector_arn = request.tenant.get_connector_arn(connector_id)
        configuration = self._validator.validate(payload.connector_configuration)
        encrypted = self._codec.encrypt(EncryptConfigurationRequest(configuration, connector_arn))
        runtime_identity = RuntimeRoleIdentity(request.tenant, connector_id)
        runtime_role_arn = self._runtime_roles.create(
            RuntimeRolePolicyRequest(
                identity=runtime_identity,
                source_role_arn=payload.source_access.role_arn,
                rest_api_id=_require_rest_api_id(request.rest_api_id),
            )
        )
        try:
            connector = self._dao.create_connector(
                CreateConnectorRequest(
                    request.tenant,
                    connector_id,
                    payload,
                    encrypted,
                    runtime_role_arn,
                )
            )
        except Exception:
            self._runtime_roles.delete(runtime_identity)
            raise
        if payload.schedule is not None:
            schedule_identity = ScheduleIdentity(request.tenant, connector_id)
            try:
                self._schedule_lifecycle.create(
                    ScheduleLifecycleRequest(
                        identity=schedule_identity,
                        schedule=payload.schedule,
                    )
                )
            except Exception:
                self._schedule_lifecycle.delete(schedule_identity)
                # delete_connector requires a deletion claim, so the rollback
                # takes the same claim the delete operation would.
                self._dao.claim_connector_deletion(ClaimConnectorDeletionRequest(request.tenant, connector_id))
                self._dao.delete_connector(DeleteConnectorRequest(request.tenant, connector_id))
                self._runtime_roles.delete(runtime_identity)
                raise
        return CreateCustomConnectorResponseContent(connector=connector)


class GetCustomConnectorActivity:
    """Fetch and decrypt one authorized connector view."""

    def __init__(
        self,
        dao: CustomConnectorsDao,
        codec: ConfigurationCodec,
        service_reference_configuration: ServiceReferenceConfiguration,
    ) -> None:
        self._dao = dao
        self._codec = codec
        self._service_reference_configuration = service_reference_configuration

    def fetch(
        self,
        request: OperationRequest[GetCustomConnectorRequest],
    ) -> GetCustomConnectorResponseContent:
        """Return connector configuration only after tenant-scoped lookup."""
        stored = self._dao.get_connector(GetConnectorRequest(request.tenant, request.payload.connector_id))
        configuration = self._codec.decrypt(DecryptConfigurationRequest(stored.encrypted_configuration, stored.arn))
        detail = CustomConnectorDetail(
            connector_id=stored.connector_id,
            arn=stored.arn,
            name=stored.name,
            created_at=stored.created_at,
            updated_at=stored.updated_at,
            status=stored.status,
            description=stored.description,
            container_properties=stored.container_properties,
            connector_configuration=configuration,
            source_access=stored.source_access,
            schedule=stored.schedule,
        )
        deployment = self._service_reference_configuration
        service_endpoint = deployment.endpoint or request.service_endpoint
        if service_endpoint is None:
            raise RuntimeError("Service endpoint is unavailable")
        service_reference = CcfServiceReference(
            endpoint=service_endpoint,
            region=deployment.region,
            service_principal=deployment.service_principal,
            service_account_id=deployment.account_id,
            destination=DeploymentDestination(
                mode=deployment.destination_mode,
                bucket_arn=(f"arn:{deployment.partition}:s3:::{deployment.ingestion_bucket_name}"),
                object_prefix=create_object_prefix(request.tenant, stored.connector_id),
            ),
        )
        return GetCustomConnectorResponseContent(
            connector=detail,
            service_reference=service_reference,
        )


class ListCustomConnectorsActivity:
    """List connector summaries for one tenant."""

    def __init__(self, dao: CustomConnectorsDao) -> None:
        self._dao = dao

    def list(
        self,
        request: OperationRequest[ListCustomConnectorsRequest],
    ) -> ListCustomConnectorsResponseContent:
        """List connectors without configuration or ciphertext."""
        stored = self._dao.list_connectors(
            ListConnectorsRequest(
                request.tenant,
                request.payload.max_results or 50,
                request.payload.next_token,
            )
        )
        return ListCustomConnectorsResponseContent(
            connectors=stored.connectors,
            next_token=stored.next_token,
        )


@dataclass(frozen=True)
class ConnectorUpdateScope:
    """Which reconciled values one update request supplied."""

    source_access_supplied: bool
    schedule_supplied: bool
    previous: StoredConnector | None


class UpdateCustomConnectorActivity:
    """Verify ownership, replace supplied fields, and re-scope the runtime role."""

    def __init__(
        self,
        dao: CustomConnectorsDao,
        validator: ConfigurationValidator,
        codec: ConfigurationCodec,
        schedule_lifecycle: ScheduleLifecycle,
        ownership_validator: ConnectorOwnershipValidator,
        runtime_roles: ConnectorRuntimeRoleLifecycle,
    ) -> None:
        self._dao = dao
        self._validator = validator
        self._codec = codec
        self._schedule_lifecycle = schedule_lifecycle
        self._ownership_validator = ownership_validator
        self._runtime_roles = runtime_roles

    def update(
        self,
        request: OperationRequest[UpdateCustomConnectorRequest],
    ) -> UpdateCustomConnectorResponseContent:
        """Update a connector, replacing configuration without history."""
        payload = request.payload
        container_properties = payload.container_properties
        self._ownership_validator.verify(
            VerifyConnectorOwnershipRequest(
                tenant=request.tenant,
                image_uri=None if container_properties is None else container_properties.image_uri,
                source_access=payload.source_access,
            )
        )
        scope = self._resolve_scope(request)
        encrypted: bytes | None = None
        if payload.connector_configuration is not None:
            configuration = self._validator.validate(payload.connector_configuration)
            resource_arn = request.tenant.get_connector_arn(payload.connector_id)
            encrypted = self._codec.encrypt(EncryptConfigurationRequest(configuration, resource_arn))
        connector = self._dao.update_connector(UpdateConnectorRequest(request.tenant, payload, encrypted))
        try:
            self._reconcile(request, scope)
        except Exception:
            self._restore(request, scope)
            raise
        return UpdateCustomConnectorResponseContent(connector=connector)

    def _resolve_scope(self, request: OperationRequest[UpdateCustomConnectorRequest]) -> ConnectorUpdateScope:
        payload = request.payload
        schedule_supplied = "schedule" in payload.model_fields_set
        source_access_supplied = payload.source_access is not None
        previous = (
            self._dao.get_connector(GetConnectorRequest(request.tenant, payload.connector_id))
            if schedule_supplied or source_access_supplied
            else None
        )
        return ConnectorUpdateScope(
            source_access_supplied=source_access_supplied,
            schedule_supplied=schedule_supplied,
            previous=previous,
        )

    def _reconcile(
        self,
        request: OperationRequest[UpdateCustomConnectorRequest],
        scope: ConnectorUpdateScope,
    ) -> None:
        payload = request.payload
        if scope.source_access_supplied and payload.source_access is not None:
            self._replace_runtime_policy(request, payload.source_access)
        if scope.schedule_supplied:
            self._replace_schedule(ScheduleIdentity(request.tenant, payload.connector_id), payload.schedule)

    def _restore(
        self,
        request: OperationRequest[UpdateCustomConnectorRequest],
        scope: ConnectorUpdateScope,
    ) -> None:
        previous = scope.previous
        if previous is None:
            return
        connector_id = request.payload.connector_id
        rollback_values: dict[str, object] = {"connector_id": connector_id}
        if scope.source_access_supplied:
            self._replace_runtime_policy(request, previous.source_access)
            rollback_values["source_access"] = previous.source_access
        if scope.schedule_supplied:
            self._replace_schedule(ScheduleIdentity(request.tenant, connector_id), previous.schedule)
            rollback_values["schedule"] = previous.schedule
        self._dao.update_connector(
            UpdateConnectorRequest(
                request.tenant,
                UpdateCustomConnectorRequest.model_validate(rollback_values),
                None,
            )
        )

    def _replace_runtime_policy(
        self,
        request: OperationRequest[UpdateCustomConnectorRequest],
        source_access: SourceAccessConfiguration,
    ) -> None:
        self._runtime_roles.replace_policy(
            RuntimeRolePolicyRequest(
                identity=RuntimeRoleIdentity(request.tenant, request.payload.connector_id),
                source_role_arn=source_access.role_arn,
                rest_api_id=_require_rest_api_id(request.rest_api_id),
            )
        )

    def _replace_schedule(
        self,
        identity: ScheduleIdentity,
        schedule: ConnectorSchedule | None,
    ) -> None:
        if schedule is None:
            self._schedule_lifecycle.delete(identity)
            return
        self._schedule_lifecycle.update(ScheduleLifecycleRequest(identity=identity, schedule=schedule))


class DeleteCustomConnectorActivity:
    """Delete one idle connector, its runtime role, and its service-owned schedule."""

    def __init__(
        self,
        dao: CustomConnectorsDao,
        schedule_lifecycle: ScheduleLifecycle,
        runtime_roles: ConnectorRuntimeRoleLifecycle,
    ) -> None:
        self._dao = dao
        self._schedule_lifecycle = schedule_lifecycle
        self._runtime_roles = runtime_roles

    def delete(self, request: OperationRequest[DeleteCustomConnectorRequest]) -> None:
        """Claim deletion, revoke runtime access, then delete schedule and record."""
        connector_id = request.payload.connector_id
        rest_api_id = _require_rest_api_id(request.rest_api_id)
        stored = self._dao.get_connector(GetConnectorRequest(request.tenant, connector_id))
        claim = ClaimConnectorDeletionRequest(request.tenant, connector_id)
        # The claim is the authority for idleness: it fails closed when the
        # connector is reserved and blocks any later reservation, so no job can
        # start between this point and the record delete.
        self._dao.claim_connector_deletion(claim)

        runtime_identity = RuntimeRoleIdentity(request.tenant, connector_id)
        schedule_identity = ScheduleIdentity(request.tenant, connector_id)
        self._runtime_roles.delete(runtime_identity)
        try:
            self._schedule_lifecycle.delete(schedule_identity)
            self._dao.delete_connector(DeleteConnectorRequest(request.tenant, connector_id))
        except Exception:
            # The record still exists, so it must not be left without the
            # runtime role or the schedule it is registered with.
            self._restore(runtime_identity, schedule_identity, stored, rest_api_id)
            self._dao.release_connector_deletion(claim)
            raise

    def _restore(
        self,
        runtime_identity: RuntimeRoleIdentity,
        schedule_identity: ScheduleIdentity,
        stored: StoredConnector,
        rest_api_id: str,
    ) -> None:
        self._runtime_roles.create(
            RuntimeRolePolicyRequest(
                identity=runtime_identity,
                source_role_arn=stored.source_access.role_arn,
                rest_api_id=rest_api_id,
            )
        )
        if stored.schedule is not None:
            self._schedule_lifecycle.create(
                ScheduleLifecycleRequest(identity=schedule_identity, schedule=stored.schedule)
            )
