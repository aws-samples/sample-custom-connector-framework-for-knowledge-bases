"""Who a request acts for: a tenant principal, or a custom connector's runtime role.

A tenant principal is a SigV4 caller from an allowed tenant account that sends no
`tenant_arn_prefix` and isn't a runtime role. A runtime role sends `tenant_arn_prefix`; it counts
only when the caller is the runtime role stored for the custom connector the request names
(`connector_id`, in the path or a header), in the request's destination. A runtime role that leaves
`tenant_arn_prefix` out is refused rather than treated as the service account's tenant. Every
refusal raises `UnauthorizedCallerError`.
"""

from __future__ import annotations

from ccf_api_models.base import ValueModel
from ccf_api_models.catalog import Caller
from ccf_api_models.identifiers import ConnectorRef, DestinationRef, TenantContext
from ccf_api_models.models import ConnectorId, DestinationId, TenantArnPrefix

from common.authenticated_call import RoleIdentity
from common.errors import ResourceNotFoundError, UnauthorizedCallerError
from common.operation import ResolvedCaller, ResolveTenantRequest
from common.settings import ApiSettings
from common.storage.ddb.custom_connectors_dao import CustomConnectorsDao


class _Scope(ValueModel):
    """The members of a request that say whom it acts for."""

    destination_id: DestinationId | None = None
    connector_id: ConnectorId | None = None
    tenant_arn_prefix: TenantArnPrefix | None = None

    @classmethod
    def of(cls, request: ResolveTenantRequest) -> _Scope:
        return cls.model_validate(request.payload.model_dump(include=set(cls.model_fields)))


class TenantResolver:
    def __init__(self, connectors: CustomConnectorsDao, settings: ApiSettings) -> None:
        self._connectors = connectors
        self._service = settings.service
        self._runtime_role_prefix = settings.runtime_roles.role_name_prefix
        self._allowed_accounts = frozenset({settings.service_account_id, *settings.allowed_tenant_account_ids})

    def resolve(self, request: ResolveTenantRequest) -> ResolvedCaller:
        if _Scope.of(request).tenant_arn_prefix is None:
            return self._tenant_principal(request)
        return self._runtime_role(request)

    def _tenant_principal(self, request: ResolveTenantRequest) -> ResolvedCaller:
        caller = request.call.tenant(self._service)
        if Caller.TENANT not in request.callers:
            raise UnauthorizedCallerError("The operation admits only a custom connector's runtime role")
        if self._is_runtime_role(request):
            raise UnauthorizedCallerError("A runtime role must name the tenant it acts for")
        if caller.account_id not in self._allowed_accounts:
            raise UnauthorizedCallerError("The caller's account is not an allowed tenant account")
        return ResolvedCaller(tenant=caller)

    def _runtime_role(self, request: ResolveTenantRequest) -> ResolvedCaller:
        scope = _Scope.of(request)
        if Caller.RUNTIME not in request.callers:
            raise UnauthorizedCallerError("The operation admits only tenant principals")
        if request.call.account_id != self._service.account_id:
            raise UnauthorizedCallerError("Only the service account's runtime roles may delegate")
        if scope.connector_id is None or scope.destination_id is None or scope.tenant_arn_prefix is None:
            raise UnauthorizedCallerError("A delegated call must name its custom connector")
        tenant = TenantContext.from_arn_prefix(scope.tenant_arn_prefix)
        destination = DestinationRef(tenant=tenant, destination_id=scope.destination_id)
        try:
            stored = self._connectors.get(ConnectorRef(destination=destination, connector_id=scope.connector_id))
        except ResourceNotFoundError as error:
            raise UnauthorizedCallerError("The named custom connector is not in the request's destination") from error
        if RoleIdentity.from_role_arn(stored.runtime_role_arn) != request.call.role_identity():
            raise UnauthorizedCallerError("The caller is not the custom connector's runtime role")
        return ResolvedCaller(tenant=tenant, connector=stored)

    def _is_runtime_role(self, request: ResolveTenantRequest) -> bool:
        if request.call.account_id != self._service.account_id:
            return False
        try:
            role = request.call.role_identity()
        except UnauthorizedCallerError:
            return False
        return role.role_name.startswith(self._runtime_role_prefix)
