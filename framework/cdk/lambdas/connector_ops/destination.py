"""The custom resource that creates a `Destination`, waits until it is `ACTIVE` with its metadata fields, and deletes
it again."""

from __future__ import annotations

import boto3
from ccf_api_models.models import (
    CreateDestinationRequest,
    DeleteDestinationRequest,
    Destination,
    DestinationStatus,
    DestinationType,
    ErrorCode,
    GetDestinationRequest,
    MetadataField,
    MetadataFieldSearchability,
    MetadataFieldType,
    UpdateDestinationRequest,
)
from ccf_api_models.operations import CcfApi
from custom_connector_framework.client import CcfApiError, CcfClientSettings, create_ccf_client
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


class MetadataFieldProperty(CustomResourceModel):
    """One `MetadataFields` entry, in CloudFormation's PascalCase."""

    name: str
    type: MetadataFieldType
    searchability: MetadataFieldSearchability

    def field(self) -> MetadataField:
        return MetadataField(name=self.name, type=self.type, searchability=self.searchability)


class DestinationProperties(CustomResourceProperties):
    name: str
    type: DestinationType
    role_arn: str
    access_control_enabled: bool = False
    metadata_fields: tuple[MetadataFieldProperty, ...] = ()

    def fields(self) -> list[MetadataField]:
        return [field.field() for field in self.metadata_fields]

    def replacing_members(self) -> dict[str, object]:
        """The members whose change replaces the destination: all but `ServiceToken` and `MetadataFields`."""
        return self.model_dump(exclude={"service_token", "metadata_fields"})


class DestinationAttributes(CustomResourceModel):
    destination_id: str
    # CloudFormation's `Fn::GetAtt` fails on a missing attribute, so an `S3` or `QUICK` destination's IDs are "".
    bucket_name: str = ""
    knowledge_base_id: str = ""
    data_source_id: str = ""

    @classmethod
    def from_destination(cls, destination: Destination) -> DestinationAttributes:
        resources = destination.destination_resources
        return cls(
            destination_id=destination.destination_id,
            bucket_name=resources.bucket_name or "",
            knowledge_base_id=resources.knowledge_base_id or "",
            data_source_id=resources.data_source_id or "",
        )


type DestinationEvent = CustomResourceEvent[DestinationProperties]
type DestinationResponse = CustomResourceResponse[DestinationAttributes]


class _FieldsCheck(BaseModel):
    """An `ACTIVE` destination and the event whose properties name the fields it should have."""

    model_config = ConfigDict(frozen=True)

    destination: Destination
    event: CustomResourceEvent[DestinationProperties]


class DestinationCalls:
    """A change to `MetadataFields` alone keeps the destination, any other change replaces it. `is_ready` sets the
    fields once the destination is `ACTIVE`, and the delete runs from `is_gone`."""

    def __init__(self, api: CcfApi) -> None:
        self._api = api

    def create(self, event: DestinationEvent) -> DestinationResponse:
        properties = event.resource_properties
        created = self._api.call(
            CreateDestinationRequest(
                name=properties.name,
                type=properties.type,
                role_arn=properties.role_arn,
                access_control_enabled=properties.access_control_enabled,
                client_token=event.client_token(),
            )
        ).destination
        return CustomResourceResponse(
            physical_resource_id=created.destination_id,
            data=DestinationAttributes(destination_id=created.destination_id),
        )

    def update(self, event: DestinationEvent) -> DestinationResponse:
        old, new = event.old_resource_properties, event.resource_properties
        if old is None or old.replacing_members() != new.replacing_members():
            return self.create(event)
        destination = self._read(event)
        return CustomResourceResponse(
            physical_resource_id=destination.destination_id, data=DestinationAttributes.from_destination(destination)
        )

    def delete(self, event: DestinationEvent) -> None:
        """No call: `is_gone` deletes once a create still running has ended."""

    def is_ready(self, event: DestinationEvent) -> CompletionResponse[DestinationAttributes]:
        """Complete at `ACTIVE` with the properties' metadata fields; `FAILED` fails the event with the status
        reason, and the rollback deletes."""
        destination = self._read(event)
        match destination.status:
            case DestinationStatus.ACTIVE:
                return self._with_fields(_FieldsCheck(destination=destination, event=event))
            case DestinationStatus.FAILED:
                raise RuntimeError(destination.status_reason or "The destination workflow failed")
            case _:
                return CompletionResponse(is_complete=False)

    def _with_fields(self, check: _FieldsCheck) -> CompletionResponse[DestinationAttributes]:
        """Set the fields when they differ; the knowledge base still applying an earlier update means not yet."""
        destination, fields = check.destination, check.event.resource_properties.fields()
        if destination.metadata_fields != fields:
            try:
                self._api.call(
                    UpdateDestinationRequest(destination_id=destination.destination_id, metadata_fields=fields)
                )
            except CcfApiError as error:
                if error.code is ErrorCode.CONFLICT:
                    return CompletionResponse(is_complete=False)
                raise
        return CompletionResponse(is_complete=True, data=DestinationAttributes.from_destination(destination))

    def is_gone(self, event: DestinationEvent) -> bool:
        """Wait while it is created or deleted, delete it at `ACTIVE` or `FAILED`; the handler ends at not found."""
        destination = self._read(event)
        if destination.status in {DestinationStatus.ACTIVE, DestinationStatus.FAILED}:
            self._api.call(DeleteDestinationRequest(destination_id=destination.destination_id))
        return False

    def _read(self, event: DestinationEvent) -> Destination:
        return self._api.call(GetDestinationRequest(destination_id=event.existing_id())).destination


_SETTINGS = ConnectorOpsSettings()
_HANDLER = CustomResourceHandler(
    DestinationCalls(
        create_ccf_client(
            CcfClientSettings(
                endpoint=_SETTINGS.framework_endpoint, region=_SETTINGS.service_region, session=boto3.Session()
            )
        )
    ),
    DestinationProperties,
)


def on_event(event: dict[str, object], context: object) -> dict[str, object]:
    return _HANDLER.on_event(event, context)


def is_complete(event: dict[str, object], context: object) -> dict[str, object]:
    return _HANDLER.is_complete(event, context)
