"""The framework API client: one `call`, typed per operation by the generated `CcfApi` overloads."""

from __future__ import annotations

import uuid
from http import HTTPStatus
from typing import Any

from botocore.httpsession import URLLib3Session
from ccf_api_models.base import CcfModel
from ccf_api_models.catalog import HeaderMember, OperationCatalog, ParameterLocation
from ccf_api_models.models import ErrorCode
from ccf_api_models.operations import CcfApi
from pydantic import ValidationError

from custom_connector_framework.client.settings import CcfClientSettings
from custom_connector_framework.client.transport import ApiFailure, CcfApiError, OutgoingCall, RetryPolicy, Transport

_CONNECTOR_ID_MEMBER = "connector_id"


class CcfClient(CcfApi):
    """Calls the framework API. Build one with `create_ccf_client`."""

    def __init__(self, catalog: OperationCatalog, transport: Transport, settings: CcfClientSettings) -> None:
        self._catalog = catalog
        self._transport = transport
        # Private: the settings hold the session whose credentials sign every call.
        self._settings = settings

    def call(self, request: CcfModel) -> Any:
        """Send the request's operation and return its parsed response, or None for an operation without one."""
        spec = self._catalog.spec_for(type(request))
        content = self._transport.send(self._filled(OutgoingCall(spec=spec, members=request.model_dump_for_wire())))
        if spec.response_model is None:
            return None
        try:
            return spec.response_model.model_validate_json(content)
        except ValidationError as error:
            failure = ApiFailure(
                status_code=HTTPStatus.OK,
                code=ErrorCode.INTERNAL_SERVER_ERROR,
                message=f"The framework's {spec.operation.value} response isn't the modeled response",
            )
            raise CcfApiError(failure) from error

    def _filled(self, call: OutgoingCall) -> OutgoingCall:
        """The call with a new client token, the settings' correlation ID and delegation, and, on a delegated
        call whose path doesn't hold it, the custom connector header; a member the request set is kept."""
        spec = call.spec
        filled: dict[str, object | None] = {HeaderMember.CORRELATION_ID: self._settings.correlation_id}
        if spec.client_token_member is not None:
            filled[spec.client_token_member] = str(uuid.uuid4())
        delegation = self._settings.delegation
        if delegation is not None:
            filled[HeaderMember.TENANT_ARN_PREFIX] = delegation.tenant_arn_prefix
            binding = spec.binding_of(_CONNECTOR_ID_MEMBER)
            if binding is not None and binding.location is ParameterLocation.HEADER:
                filled[_CONNECTOR_ID_MEMBER] = delegation.connector_id
        members = dict(call.members)
        for name, value in filled.items():
            if value is not None and members.get(name) is None:
                members[name] = value
        return OutgoingCall(spec=spec, members=members)


def create_ccf_client(settings: CcfClientSettings) -> CcfApi:
    """Build a client over the operations in the `openapi.json` packaged with ccf-api-models."""
    transport = Transport(settings, URLLib3Session(), RetryPolicy(settings.retry))
    return CcfClient(OperationCatalog.load(), transport, settings)
