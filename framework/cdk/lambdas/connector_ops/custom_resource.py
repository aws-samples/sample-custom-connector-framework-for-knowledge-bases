"""What every `connector_ops` custom resource shares: CloudFormation's event and response shapes, and the handler
that runs one resource's create, update, delete, and completion checks.

`on_event` and `is_complete` take the Lambda runtime's two parameters. A delete whose call, or whose completion
check, gets `RESOURCE_NOT_FOUND` from the framework counts as deleted, so no resource repeats that catch.
"""

import hashlib
from collections.abc import Mapping
from enum import StrEnum
from typing import Protocol

from ccf_api_models.models import ErrorCode
from custom_connector_framework.client import CcfApiError
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_pascal


class CloudFormationRequestType(StrEnum):
    CREATE = "Create"
    UPDATE = "Update"
    DELETE = "Delete"


class CustomResourceModel(BaseModel):
    """CloudFormation's PascalCase members. Numbers and booleans arrive as strings, which lax parsing converts.

    The event envelopes ignore members they don't read, because CloudFormation adds its own.
    """

    model_config = ConfigDict(frozen=True, alias_generator=to_pascal, populate_by_name=True, extra="ignore")


class CustomResourceProperties(CustomResourceModel):
    """A custom resource's `ResourceProperties`: the construct's members and CloudFormation's `ServiceToken`.

    An unknown member, such as a misspelled one, fails the event.
    """

    model_config = ConfigDict(extra="forbid")

    service_token: str

    def without_service_token(self) -> dict[str, object]:
        """The construct's members, so a replaced provider alone doesn't count as a change."""
        return self.model_dump(exclude={"service_token"})


class CustomResourceEvent[PropertiesT: CustomResourceProperties](CustomResourceModel):
    request_type: CloudFormationRequestType
    stack_id: str
    logical_resource_id: str
    resource_properties: PropertiesT
    old_resource_properties: PropertiesT | None = None
    physical_resource_id: str | None = None
    data: dict[str, str] = {}
    """The `Data` `on_event` returned, which the provider passes to `is_complete`."""

    def existing_id(self) -> str:
        """The physical ID CloudFormation sends with every Update and Delete, and the provider with `is_complete`."""
        if self.physical_resource_id is None:
            raise ValueError("PhysicalResourceId is required for Update, Delete, and isComplete")
        return self.physical_resource_id

    def client_token(self) -> str:
        """The SHA-256 of the stack ID and logical ID, so a retried create returns the resource the first one made.

        A replacement adds the replaced resource's physical ID, because the framework rejects one token used for
        two different requests.
        """
        parts = [self.stack_id, self.logical_resource_id]
        if self.request_type is CloudFormationRequestType.UPDATE and self.physical_resource_id is not None:
            parts.append(self.physical_resource_id)
        return hashlib.sha256("\0".join(parts).encode()).hexdigest()

    def properties_changed(self) -> bool:
        """Whether the old and new properties differ, leaving out `ServiceToken`."""
        old = self.old_resource_properties
        return old is None or old.without_service_token() != self.resource_properties.without_service_token()


class CustomResourceResponse[AttributesT: CustomResourceModel](CustomResourceModel):
    physical_resource_id: str
    data: AttributesT | None = None


class CompletionResponse[AttributesT: CustomResourceModel](CustomResourceModel):
    is_complete: bool
    data: AttributesT | None = None


class CustomResource[
    PropertiesT: CustomResourceProperties,
    AttributesT: CustomResourceModel,
](Protocol):
    """What one custom resource supplies; `CustomResourceHandler` decides which to call."""

    def create(self, event: CustomResourceEvent[PropertiesT]) -> CustomResourceResponse[AttributesT]: ...

    def update(self, event: CustomResourceEvent[PropertiesT]) -> CustomResourceResponse[AttributesT]: ...

    def delete(self, event: CustomResourceEvent[PropertiesT]) -> None: ...

    def is_ready(self, event: CustomResourceEvent[PropertiesT]) -> CompletionResponse[AttributesT]: ...

    def is_gone(self, event: CustomResourceEvent[PropertiesT]) -> bool: ...


class CustomResourceHandler[
    PropertiesT: CustomResourceProperties,
    AttributesT: CustomResourceModel,
]:
    """The provider's `onEvent` and `isComplete` entry points for one resource."""

    def __init__(
        self,
        resource: CustomResource[PropertiesT, AttributesT],
        properties_type: type[PropertiesT],
    ) -> None:
        self._resource = resource
        self._event_type = CustomResourceEvent[properties_type]  # type: ignore[valid-type]

    def on_event(self, event: Mapping[str, object], _context: object) -> dict[str, object]:
        parsed: CustomResourceEvent[PropertiesT] = self._event_type.model_validate(event)
        response: CustomResourceResponse[AttributesT]
        match parsed.request_type:
            case CloudFormationRequestType.CREATE:
                response = self._resource.create(parsed)
            case CloudFormationRequestType.UPDATE:
                response = self._resource.update(parsed)
            case CloudFormationRequestType.DELETE:
                self._delete(parsed)
                response = CustomResourceResponse(physical_resource_id=parsed.existing_id())
        return response.model_dump(by_alias=True, exclude_none=True)

    def is_complete(self, event: Mapping[str, object], _context: object) -> dict[str, object]:
        parsed: CustomResourceEvent[PropertiesT] = self._event_type.model_validate(event)
        completion: CompletionResponse[AttributesT]
        match parsed.request_type:
            case CloudFormationRequestType.CREATE | CloudFormationRequestType.UPDATE:
                completion = self._resource.is_ready(parsed)
            case CloudFormationRequestType.DELETE:
                completion = CompletionResponse(is_complete=self._is_gone(parsed))
        return completion.model_dump(by_alias=True, exclude_none=True)

    def _is_gone(self, event: CustomResourceEvent[PropertiesT]) -> bool:
        try:
            return self._resource.is_gone(event)
        except CcfApiError as error:
            if error.code is ErrorCode.RESOURCE_NOT_FOUND:
                return True
            raise

    def _delete(self, event: CustomResourceEvent[PropertiesT]) -> None:
        try:
            self._resource.delete(event)
        except CcfApiError as error:
            if error.code is not ErrorCode.RESOURCE_NOT_FOUND:
                raise
