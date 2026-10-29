"""Typed request envelope shared by API activities and storage DAOs."""

from __future__ import annotations

from dataclasses import dataclass

from ccf_api_models.base import CcfModel

from common.tenant import TenantContext


@dataclass(frozen=True)
class OperationRequest[RequestModelT: CcfModel]:
    """Tenant identity paired with one generated operation request."""

    tenant: TenantContext
    payload: RequestModelT
    idempotency_key: str | None = None
    service_endpoint: str | None = None
    rest_api_id: str | None = None
