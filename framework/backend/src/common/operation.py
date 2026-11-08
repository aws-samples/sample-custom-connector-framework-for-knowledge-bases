"""The request envelope every API activity takes, and the tenant resolution the router runs before it."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from ccf_api_models.base import CcfModel, ValueModel
from ccf_api_models.catalog import Caller
from ccf_api_models.identifiers import TenantContext
from ccf_api_models.models import CorrelationId
from pydantic import ConfigDict

from common.authenticated_call import AuthenticatedCall
from common.storage.ddb.custom_connectors_dao import ConnectorItem

type Activity = Callable[[OperationRequest[Any]], CcfModel | None]
"""One operation's activity; `Any` because one map holds the 22 activities over 22 request types."""


class RequestTrace(ValueModel):
    """The identifiers that trace a workflow execution back to the API call that started it."""

    correlation_id: CorrelationId
    """The caller's `x-correlation-id`, or a new UUID when it sent none."""
    api_request_id: str


class OperationRequest[PayloadT: CcfModel](ValueModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    tenant: TenantContext
    payload: PayloadT
    trace: RequestTrace
    connector: ConnectorItem | None = None
    """The custom connector the resolver read for a runtime role's call; None for a tenant principal."""


class ResolveTenantRequest(ValueModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    call: AuthenticatedCall
    payload: CcfModel
    callers: frozenset[Caller]


class ResolvedCaller(ValueModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    tenant: TenantContext
    connector: ConnectorItem | None = None


class TenantResolution(Protocol):
    """Returns who a request acts for, or raises UnauthorizedCallerError."""

    def resolve(self, request: ResolveTenantRequest) -> ResolvedCaller: ...
