"""CloudFormation custom-resource handler for CCF connector registration."""

import json
from collections.abc import Callable
from typing import Any

from ccf import client
from ccf_api_models.models import (
    ConnectorSchedule,
    ContainerProperties,
    CreateCustomConnectorRequest,
    DeleteCustomConnectorRequest,
    ErrorCode,
    ResourceRequirements,
    ScheduleState,
    SourceAccessConfiguration,
    UpdateContainerProperties,
    UpdateCustomConnectorRequest,
)
from custom_connector_framework.client import CcfServiceError
from pydantic import SecretStr


class ConnectorRegistrationValidationError(ValueError):
    pass


def _schedule(properties: dict[str, Any]) -> ConnectorSchedule | None:
    expression = properties.get("ScheduleExpression")
    if not expression:
        return None
    return ConnectorSchedule(
        expression=str(expression), state=ScheduleState.ENABLED, timezone=None
    )


def _configuration(properties: dict[str, Any]) -> SecretStr:
    configuration = properties["ConnectorConfiguration"]
    if not isinstance(configuration, dict):
        raise TypeError("ConnectorConfiguration must be an object.")
    return SecretStr(json.dumps(configuration, separators=(",", ":"), sort_keys=True))


def _create_request(properties: dict[str, Any]) -> CreateCustomConnectorRequest:
    return CreateCustomConnectorRequest(
        name=properties["ConnectorName"],
        description=properties.get("Description"),
        container_properties=ContainerProperties(
            image_uri=properties["DockerImageUri"],
            command=properties["Command"],
            resource_requirements=ResourceRequirements(
                memory=int(properties["Memory"]), cpu=float(properties["Cpu"])
            ),
            timeout=int(properties["Timeout"]),
        ),
        connector_configuration=_configuration(properties),
        source_access=SourceAccessConfiguration(
            role_arn=properties["SourceRoleArn"],
            external_id=properties["SourceAccessExternalId"],
        ),
        schedule=_schedule(properties),
    )


def _update_request(
    properties: dict[str, Any], connector_id: str
) -> UpdateCustomConnectorRequest:
    create_request = _create_request(properties)
    container = create_request.container_properties
    return UpdateCustomConnectorRequest(
        connector_id=connector_id,
        name=create_request.name,
        description=create_request.description,
        container_properties=UpdateContainerProperties(
            image_uri=container.image_uri,
            command=container.command,
            resource_requirements=container.resource_requirements,
            timeout=container.timeout,
        ),
        connector_configuration=create_request.connector_configuration,
        source_access=create_request.source_access,
        schedule=_schedule(properties),
    )


def _validated_request[Request](builder: Callable[[], Request]) -> Request:
    try:
        return builder()
    except KeyError, TypeError, ValueError:
        raise ConnectorRegistrationValidationError(
            "Connector registration properties are invalid."
        ) from None


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    request_type = event.get("RequestType")
    properties = event.get("ResourceProperties", {})
    physical_id = event.get("PhysicalResourceId", "")
    api = client()
    if request_type == "Create":
        connector = api.create_custom_connector(
            _validated_request(lambda: _create_request(properties))
        ).connector
        physical_id = connector.connector_id
    elif request_type == "Update":
        connector = api.update_custom_connector(
            _validated_request(lambda: _update_request(properties, physical_id))
        ).connector
    elif request_type == "Delete":
        try:
            api.delete_custom_connector(
                _validated_request(
                    lambda: DeleteCustomConnectorRequest(connector_id=physical_id)
                )
            )
        except CcfServiceError as error:
            if error.code != ErrorCode.RESOURCE_NOT_FOUND:
                raise
        return {"PhysicalResourceId": physical_id, "Data": {}}
    else:
        raise ConnectorRegistrationValidationError(
            "Connector registration properties are invalid."
        ) from None
    return {
        "PhysicalResourceId": physical_id,
        "Data": {"ConnectorArn": connector.arn},
    }
