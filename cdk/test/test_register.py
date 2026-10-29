"""Focused verification for the connector registration Lambda."""

import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
for source in (
    ROOT / "api/packages/ccf-api-models/src",
    ROOT / "toolkit/src",
    ROOT / "cdk/lambdas/connector_ops",
):
    sys.path.insert(0, str(source))

import register
from ccf_api_models.models import (
    CreateCustomConnectorRequest,
    DeleteCustomConnectorRequest,
    ErrorCode,
    UpdateCustomConnectorRequest,
)
from custom_connector_framework import CcfClient, CcfServiceError

CONNECTOR_CONFIGURATION = {"repositories": [{"full_name": "one/two"}]}
PROPERTIES = {
    "ConnectorName": "example",
    "Description": "Example connector",
    "DockerImageUri": "123456789012.dkr.ecr.us-east-1.amazonaws.com/example:latest",
    "Command": ["python", "-m", "example"],
    "ConnectorConfiguration": CONNECTOR_CONFIGURATION,
    "SourceRoleArn": "arn:aws:iam::123456789012:role/CcfSourceRole-example",
    "SourceAccessExternalId": "ccf-example-external-id",
    "ScheduleExpression": "rate(1 day)",
    "Cpu": 1,
    "Memory": 2048,
    "Timeout": 900,
}
PHYSICAL_ID = "connector-1"
CONNECTOR_ARN = "arn:aws:ccf:us-east-1:123456789012:custom-connector/connector-1"
SECRET_MARKER = "DO-NOT-EMIT-CONNECTOR-CONFIGURATION"


def api() -> MagicMock:
    """Return a typed-client fake with successful connector responses."""
    ccf_client = MagicMock(spec=CcfClient)
    connector = SimpleNamespace(connector_id=PHYSICAL_ID, arn=CONNECTOR_ARN)
    response = SimpleNamespace(connector=connector)
    ccf_client.create_custom_connector.return_value = response
    ccf_client.update_custom_connector.return_value = response
    return ccf_client


def update(
    properties: dict[str, object], ccf_client: MagicMock
) -> UpdateCustomConnectorRequest:
    """Invoke a real update model-construction path and return its request."""
    with patch.object(register, "client", return_value=ccf_client):
        register.handler(
            {
                "RequestType": "Update",
                "PhysicalResourceId": PHYSICAL_ID,
                "ResourceProperties": properties,
            },
            None,
        )
    request = ccf_client.update_custom_connector.call_args.args[0]
    assert isinstance(request, UpdateCustomConnectorRequest)
    return request


def test_handler_uses_typed_create_update_delete_operations() -> None:
    ccf_client = api()
    with patch.object(register, "client", return_value=ccf_client):
        create_response = register.handler(
            {"RequestType": "Create", "ResourceProperties": PROPERTIES}, None
        )
        update_response = register.handler(
            {
                "RequestType": "Update",
                "PhysicalResourceId": PHYSICAL_ID,
                "ResourceProperties": PROPERTIES,
            },
            None,
        )
        delete_response = register.handler(
            {"RequestType": "Delete", "PhysicalResourceId": PHYSICAL_ID}, None
        )

    create_request = ccf_client.create_custom_connector.call_args.args[0]
    update_request = ccf_client.update_custom_connector.call_args.args[0]
    assert isinstance(create_request, CreateCustomConnectorRequest)
    assert isinstance(update_request, UpdateCustomConnectorRequest)
    assert isinstance(
        ccf_client.delete_custom_connector.call_args.args[0],
        DeleteCustomConnectorRequest,
    )
    assert json.loads(create_request.connector_configuration.get_secret_value()) == (
        CONNECTOR_CONFIGURATION
    )
    assert update_request.connector_configuration is not None
    assert json.loads(update_request.connector_configuration.get_secret_value()) == (
        CONNECTOR_CONFIGURATION
    )
    assert create_response["PhysicalResourceId"] == PHYSICAL_ID
    assert update_response["PhysicalResourceId"] == PHYSICAL_ID
    assert delete_response["PhysicalResourceId"] == PHYSICAL_ID


def test_scheduled_update_preserves_current_schedule() -> None:
    request = update(PROPERTIES, api())

    assert request.schedule is not None
    assert request.schedule.expression == "rate(1 day)"
    assert "schedule" in request.model_fields_set


def test_update_without_schedule_explicitly_clears_prior_schedule() -> None:
    properties = {
        key: value for key, value in PROPERTIES.items() if key != "ScheduleExpression"
    }
    request = update(properties, api())

    assert request.schedule is None
    assert "schedule" in request.model_fields_set
    assert request.model_fields_set == {
        "connector_id",
        "name",
        "description",
        "container_properties",
        "connector_configuration",
        "source_access",
        "schedule",
    }


def test_marker_bearing_configuration_validation_is_static_and_silent(
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ccf_client = api()
    malformed_properties = {
        **PROPERTIES,
        "ConnectorConfiguration": {"marker": SECRET_MARKER * 5000},
    }

    with (
        patch.object(register, "client", return_value=ccf_client),
        caplog.at_level(logging.DEBUG),
        pytest.raises(register.ConnectorRegistrationValidationError) as raised,
    ):
        register.handler(
            {"RequestType": "Create", "ResourceProperties": malformed_properties},
            None,
        )

    captured = capsys.readouterr()
    emitted = f"{captured.out}\n{captured.err}\n{caplog.text}"
    assert str(raised.value) == "Connector registration properties are invalid."
    assert SECRET_MARKER not in repr(raised.value)
    assert SECRET_MARKER not in emitted
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True
    assert emitted == "\n\n"
    ccf_client.create_custom_connector.assert_not_called()


def test_delete_ignores_only_resource_not_found() -> None:
    ccf_client = api()
    ccf_client.delete_custom_connector.side_effect = CcfServiceError(
        404, ErrorCode.RESOURCE_NOT_FOUND, "Connector not found", "request-1"
    )

    with patch.object(register, "client", return_value=ccf_client):
        response = register.handler(
            {"RequestType": "Delete", "PhysicalResourceId": PHYSICAL_ID}, None
        )

    assert response["PhysicalResourceId"] == PHYSICAL_ID


def test_delete_rethrows_other_service_errors() -> None:
    ccf_client = api()
    ccf_client.delete_custom_connector.side_effect = CcfServiceError(
        409, ErrorCode.CONFLICT, "Connector in use", "request-2"
    )

    with (
        patch.object(register, "client", return_value=ccf_client),
        pytest.raises(CcfServiceError, match="CONFLICT"),
    ):
        register.handler(
            {"RequestType": "Delete", "PhysicalResourceId": PHYSICAL_ID}, None
        )
