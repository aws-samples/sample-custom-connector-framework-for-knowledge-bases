"""The custom resource that registers one custom connector with the framework."""

from __future__ import annotations

from enum import StrEnum

import boto3
from ccf_api_models.models import (
    Connector,
    CreateCustomConnectorRequest,
    CreatedConnector,
    DeleteCustomConnectorRequest,
    GetCustomConnectorRequest,
    UpdateCustomConnectorRequest,
)
from ccf_api_models.operations import CcfApi
from custom_connector_framework.client import CcfClientSettings, create_ccf_client
from pydantic import BaseModel, ConfigDict

from ccf import ConnectorOpsSettings
from custom_resource import (
    CompletionResponse,
    CustomResourceEvent,
    CustomResourceHandler,
    CustomResourceModel,
    CustomResourceProperties,
    CustomResourceResponse,
)


class ClearableMember(StrEnum):
    """The `UpdateCustomConnector` members null removes; the generated model can't tell them from the others."""

    LOG_DESTINATION = "log_destination"
    SCHEDULE = "schedule"


class RegistrationProperties(CustomResourceProperties):
    """The `CreateCustomConnector` body `CustomConnector` sends, as JSON without a client token."""

    request: str

    @property
    def destination_id(self) -> str:
        return CreateCustomConnectorRequest.model_validate_json(self.request).destination_id

    def create_request(self, client_token: str) -> CreateCustomConnectorRequest:
        return CreateCustomConnectorRequest.model_validate_json(self.request).model_copy(
            update={"client_token": client_token}
        )

    def update_request(self, update: UpdateRequestInput) -> UpdateCustomConnectorRequest:
        """The create body plus the custom connector's ID, with null for each clearable member the new body drops."""
        body = CreateCustomConnectorRequest.model_validate_json(self.request)
        dropped = {}
        if update.old is not None:
            old = CreateCustomConnectorRequest.model_validate_json(update.old.request)
            dropped = {
                member.value: None
                for member in ClearableMember
                if getattr(old, member.value) is not None and getattr(body, member.value) is None
            }
        return UpdateCustomConnectorRequest.model_validate(
            {**body.model_dump(exclude_unset=True), "connector_id": update.connector_id, **dropped}
        )


class UpdateRequestInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    connector_id: str
    old: RegistrationProperties | None
    """The properties before the update, which say whether a log destination or schedule was removed."""


class RegistrationAttributes(CustomResourceModel):
    """What the source-access and log-delivery roles' trust policies name."""

    connector_id: str
    connector_arn: str
    runtime_role_arn: str
    external_id: str

    @classmethod
    def of(cls, connector: CreatedConnector | Connector) -> RegistrationAttributes:
        return cls(
            connector_id=connector.connector_id,
            connector_arn=connector.arn,
            runtime_role_arn=connector.runtime_role_arn,
            external_id=connector.source_access.external_id,
        )


type RegistrationEvent = CustomResourceEvent[RegistrationProperties]
type RegistrationResponse = CustomResourceResponse[RegistrationAttributes]


class ConnectorRegistration:
    """Creates, updates, or deletes the custom connector; a create or update is complete when its call returns."""

    def __init__(self, api: CcfApi) -> None:
        self._api = api

    def create(self, event: RegistrationEvent) -> RegistrationResponse:
        """A replacement's client token also names the custom connector it replaces."""
        created = self._api.call(event.resource_properties.create_request(event.client_token())).connector
        return CustomResourceResponse(
            physical_resource_id=created.connector_id, data=RegistrationAttributes.of(created)
        )

    def update(self, event: RegistrationEvent) -> RegistrationResponse:
        properties, old = event.resource_properties, event.old_resource_properties
        if old is not None and old.destination_id != properties.destination_id:
            # A custom connector can't move to another destination: create a new one; CloudFormation deletes the old.
            return self.create(event)
        request = properties.update_request(UpdateRequestInput(connector_id=event.existing_id(), old=old))
        self._api.call(request)
        connector = self._api.call(
            GetCustomConnectorRequest(destination_id=request.destination_id, connector_id=request.connector_id)
        ).connector
        return CustomResourceResponse(
            physical_resource_id=connector.connector_id, data=RegistrationAttributes.of(connector)
        )

    def delete(self, event: RegistrationEvent) -> None:
        self._api.call(
            DeleteCustomConnectorRequest(
                destination_id=event.resource_properties.destination_id, connector_id=event.existing_id()
            )
        )

    def is_ready(self, event: RegistrationEvent) -> CompletionResponse[RegistrationAttributes]:
        return CompletionResponse(is_complete=True)

    def is_gone(self, event: RegistrationEvent) -> bool:
        """The custom connector still exists while `GetCustomConnector` returns it; the handler ends at not found."""
        self._api.call(
            GetCustomConnectorRequest(
                destination_id=event.resource_properties.destination_id, connector_id=event.existing_id()
            )
        )
        return False


_SETTINGS = ConnectorOpsSettings()
_HANDLER = CustomResourceHandler(
    ConnectorRegistration(
        create_ccf_client(
            CcfClientSettings(
                endpoint=_SETTINGS.framework_endpoint, region=_SETTINGS.service_region, session=boto3.Session()
            )
        )
    ),
    RegistrationProperties,
)


def on_event(event: dict[str, object], context: object) -> dict[str, object]:
    return _HANDLER.on_event(event, context)


def is_complete(event: dict[str, object], context: object) -> dict[str, object]:
    return _HANDLER.is_complete(event, context)
