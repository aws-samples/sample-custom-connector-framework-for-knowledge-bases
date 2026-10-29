"""Tenant resolution for client callers and the service-authenticated runtime.

A client caller's tenant is derived from its SigV4 account. The framework
runtime additionally signs a tenant ARN-prefix delegation header. The header is
honoured only when the SigV4 caller is the exact runtime role persisted for the
connector named in the request path, so a client cannot claim another tenant and
one connector's runtime role cannot act for another connector.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from common.storage.ddb.custom_connectors_dao import CustomConnectorsDao, GetConnectorRequest
from common.tenant import (
    RoleIdentity,
    TenantContext,
    TenantContextError,
    extract_caller_role_identity,
    extract_tenant_context,
)

DELEGATION_HEADER = "x-ccf-tenant-arn-prefix"


@dataclass(frozen=True)
class ResolveTenantRequest:
    """One raw API Gateway event and its validated path parameters."""

    event: dict[str, Any]
    path_parameters: Mapping[str, str]


class TenantResolver:
    """Resolves the authorized tenant for one authenticated request."""

    def __init__(self, connectors_dao: CustomConnectorsDao, framework_account_id: str) -> None:
        self._connectors_dao = connectors_dao
        self._framework_account_id = framework_account_id

    def resolve(self, request: ResolveTenantRequest) -> TenantContext:
        """Return the client tenant, or a verified delegated tenant."""
        caller_tenant = extract_tenant_context(request.event)
        claimed_arn_prefix = self._delegated_arn_prefix(request.event)
        if claimed_arn_prefix is None:
            return caller_tenant
        if not self._framework_account_id or caller_tenant.account_id != self._framework_account_id:
            raise TenantContextError("Tenant delegation is not available to this caller")
        connector_id = request.path_parameters.get("connector_id")
        if not isinstance(connector_id, str) or not connector_id:
            raise TenantContextError("Tenant delegation requires a connector-scoped operation")
        claimed_tenant = TenantContext.from_arn_prefix(claimed_arn_prefix)
        caller_role = extract_caller_role_identity(request.event)
        self._verify_runtime_role(claimed_tenant, connector_id, caller_role)
        return claimed_tenant

    def _verify_runtime_role(
        self,
        claimed_tenant: TenantContext,
        connector_id: str,
        caller_role: RoleIdentity,
    ) -> None:
        stored = self._connectors_dao.get_connector(GetConnectorRequest(claimed_tenant, connector_id))
        if stored.runtime_role_arn is None:
            raise TenantContextError("Connector has no service-managed runtime role")
        if RoleIdentity.from_role_arn(stored.runtime_role_arn) != caller_role:
            raise TenantContextError("Caller is not the runtime role for this connector")

    @staticmethod
    def _delegated_arn_prefix(event: dict[str, Any]) -> str | None:
        values = _collect_header_values(event, DELEGATION_HEADER)
        if not values:
            return None
        if len(values) != 1 or not values[0]:
            raise TenantContextError("Tenant delegation header must be supplied exactly once")
        return values[0]


def _collect_header_values(event: dict[str, Any], header_name: str) -> list[str]:
    """Return every delivered value of one header across both event header maps."""
    single_values = [
        value
        for name, value in _header_items(event.get("headers"))
        if name.lower() == header_name and isinstance(value, str)
    ]
    multi_values = [
        value
        for name, entries in _header_items(event.get("multiValueHeaders"))
        if name.lower() == header_name and isinstance(entries, list)
        for value in entries
    ]
    if not multi_values:
        return single_values
    if single_values and single_values != multi_values[-1:]:
        raise TenantContextError("Tenant delegation header representations do not match")
    return [value for value in multi_values if isinstance(value, str)]


def _header_items(headers: Any) -> list[tuple[str, Any]]:
    if not isinstance(headers, dict):
        return []
    return [(name, value) for name, value in headers.items() if isinstance(name, str)]
