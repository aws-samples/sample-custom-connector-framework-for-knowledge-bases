"""One job definition per custom connector: its name, its two containers, its timeout, and its deregistration."""

from __future__ import annotations

import json
from typing import Any

import pytest
from ccf_api_models.identifiers import ConnectorRef, DestinationRef, Partition, TenantContext
from ccf_api_models.job_container import RuntimeVariable
from ccf_api_models.models import (
    ConnectorStatus,
    ContainerProperties,
    LogDestination,
    ResourceRequirements,
    SourceAccess,
)

from common.connector_job_definitions import ConnectorJobDefinitions, RegisterJobDefinitionRequest
from common.settings import JobDefinitionSettings
from common.storage.ddb.custom_connectors_dao import ConnectorItem

TENANT = TenantContext(partition=Partition.AWS, region="us-east-1", account_id="111122223333")
CONNECTOR = ConnectorRef(
    destination=DestinationRef(tenant=TENANT, destination_id="dst-7c41e9a2b5d0"), connector_id="cc-e020d65d76c0"
)
DEFINITION_ARN = "arn:aws:batch:us-east-1:444455556666:job-definition/ccf-dst-7c41e9a2b5d0-cc-e020d65d76c0:3"
SETTINGS = JobDefinitionSettings(
    name_prefix="ccf-",
    execution_role_arn="arn:aws:iam::444455556666:role/ccf/CcfBatchExecutionRole",
    api_endpoint="https://abcdef1234.execute-api.us-east-1.amazonaws.com/ccf",
    log_router_image="public.ecr.aws/aws-observability/aws-for-fluent-bit:2.34.3.20261028",
    log_router_log_group="/aws/batch/job",
    log_router_stream_prefix="ccf-log-router",
)
LOG_DESTINATION = LogDestination.model_validate_json(
    json.dumps(
        {
            "type": "CLOUDWATCH_LOGS",
            "arn": "arn:aws:logs:us-east-1:111122223333:log-group:/ccf/connectors/github-awslabs",
            "role_arn": "arn:aws:iam::111122223333:role/CcfLogDeliveryRole-github-awslabs",
        }
    )
)


class _Meta:
    region_name = "us-east-1"


class FakeBatch:
    """Records `RegisterJobDefinition` and `DeregisterJobDefinition`; `described` is what a describe returns."""

    meta = _Meta()

    def __init__(self, described: list[dict[str, Any]] | None = None) -> None:
        self.registered: list[dict[str, Any]] = []
        self.deregistered: list[str] = []
        self.described = described if described is not None else [{"status": "ACTIVE"}]

    def register_job_definition(self, **arguments: Any) -> dict[str, Any]:
        self.registered.append(arguments)
        # Each registration is the next revision; the first is the one DEFINITION_ARN names.
        return {"jobDefinitionArn": f"{DEFINITION_ARN.rsplit(':', 1)[0]}:{len(self.registered) + 2}"}

    def describe_job_definitions(self, **arguments: Any) -> dict[str, Any]:
        return {"jobDefinitions": self.described}

    def deregister_job_definition(self, **arguments: Any) -> dict[str, Any]:
        self.deregistered.append(arguments["jobDefinition"])
        return {}


def stored(log_destination: LogDestination | None = None) -> ConnectorItem:
    return ConnectorItem(
        connector_id=CONNECTOR.connector_id,
        arn=CONNECTOR.arn,
        destination_id=CONNECTOR.destination_id,
        name="github-awslabs",
        status=ConnectorStatus.AVAILABLE,
        created_at=1.0,
        updated_at=1.0,
        tenant_arn_prefix=TENANT.arn_prefix,
        destination_connector_key=CONNECTOR.destination_connector_key,
        destination_name_key=f"{CONNECTOR.destination_id}#github-awslabs",
        version=1,
        runtime_role_arn="arn:aws:iam::444455556666:role/ccf-runtime/CcfConnectorRuntime-3c9e1f0a2b7d4e6f8a1b2c3d4e5f6a7b",
        container_properties=ContainerProperties(
            image_uri="111122223333.dkr.ecr.us-east-1.amazonaws.com/ccf-connectors/github:abc",
            command=["ccf-connector", "run", "connector:GitHubConnectorImplementation"],
            resource_requirements=ResourceRequirements(cpu=2.0, memory=8192),
            timeout=43200,
        ),
        source_access=SourceAccess(
            role_arn="arn:aws:iam::111122223333:role/CcfSourceRole-github-awslabs",
            external_id="ccf-ext-2f9d41c7a8b04e1f9c3d5a6b7e8f9012",
        ),
        log_destination=log_destination,
    )


def registered(connector: ConnectorItem) -> dict[str, Any]:
    batch = FakeBatch()
    arn = ConnectorJobDefinitions(batch, SETTINGS).register(RegisterJobDefinitionRequest(connector=connector))  # type: ignore[arg-type]
    assert arn == DEFINITION_ARN
    [arguments] = batch.registered
    return arguments


def containers(arguments: dict[str, Any]) -> dict[str, dict[str, Any]]:
    [task] = arguments["ecsProperties"]["taskProperties"]
    return {container["name"]: container for container in task["containers"]}


def test_register_names_the_definition_after_the_destination_and_custom_connector() -> None:
    assert registered(stored())["jobDefinitionName"] == "ccf-dst-7c41e9a2b5d0-cc-e020d65d76c0"


def test_register_sets_the_attempt_timeout_from_the_container_properties() -> None:
    assert registered(stored())["timeout"] == {"attemptDurationSeconds": 43200}


def test_the_connector_container_gets_the_five_fixed_variables_and_no_job_id() -> None:
    environment = {item["name"]: item["value"] for item in containers(registered(stored()))["connector"]["environment"]}

    assert environment == {
        RuntimeVariable.API_ENDPOINT: SETTINGS.api_endpoint,
        RuntimeVariable.REGION: "us-east-1",
        RuntimeVariable.TENANT_ARN_PREFIX: TENANT.arn_prefix,
        RuntimeVariable.DESTINATION_ID: CONNECTOR.destination_id,
        RuntimeVariable.CONNECTOR_ID: CONNECTOR.connector_id,
    }


def test_the_connector_container_gets_the_task_size_less_the_log_routers_share_and_waits_for_it() -> None:
    connector = containers(registered(stored()))["connector"]

    assert connector["resourceRequirements"] == [{"type": "VCPU", "value": "1.75"}, {"type": "MEMORY", "value": "7680"}]
    assert connector["dependsOn"] == [{"containerName": "log-router", "condition": "START"}]


def test_without_a_log_destination_the_connector_output_is_discarded() -> None:
    assert containers(registered(stored()))["connector"]["logConfiguration"] == {
        "logDriver": "awsfirelens",
        "options": {"Name": "null"},
    }


def test_with_a_log_destination_each_job_writes_its_own_stream_in_the_tenants_log_group() -> None:
    options = containers(registered(stored(LOG_DESTINATION)))["connector"]["logConfiguration"]["options"]

    assert options["Name"] == "cloudwatch_logs"
    assert options["log_group_name"] == "/ccf/connectors/github-awslabs"
    assert options["log_stream_name"] == "${CUSTOM_CONNECTOR_FRAMEWORK_JOB_ID}"
    assert (options["role_arn"], options["external_id"]) == (
        LOG_DESTINATION.role_arn,
        "ccf-ext-2f9d41c7a8b04e1f9c3d5a6b7e8f9012",
    )


def test_the_log_router_writes_its_own_logs_where_the_settings_say() -> None:
    router = containers(registered(stored()))["log-router"]

    assert router["image"] == SETTINGS.log_router_image
    assert router["logConfiguration"]["options"] == {
        "awslogs-group": "/aws/batch/job",
        "awslogs-region": "us-east-1",
        "awslogs-stream-prefix": "ccf-log-router",
    }


@pytest.mark.parametrize("described", [[], [{"status": "INACTIVE"}]], ids=["missing", "inactive"])
def test_deregister_of_a_definition_batch_reports_missing_or_inactive_is_done(described: list[dict[str, Any]]) -> None:
    batch = FakeBatch(described)

    ConnectorJobDefinitions(batch, SETTINGS).deregister(DEFINITION_ARN)  # type: ignore[arg-type]

    assert batch.deregistered == []


def test_deregister_of_an_active_definition_deregisters_it() -> None:
    batch = FakeBatch()

    ConnectorJobDefinitions(batch, SETTINGS).deregister(DEFINITION_ARN)  # type: ignore[arg-type]

    assert batch.deregistered == [DEFINITION_ARN]
