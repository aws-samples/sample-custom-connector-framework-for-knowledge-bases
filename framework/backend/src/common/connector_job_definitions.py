"""One Batch job definition per custom connector, registered when it is created or updated.

Each job runs two essential containers: `connector`, the registered image, and `log-router`,
Fluent Bit. The connector container's output goes through FireLens to the router, which writes it
to the tenant's log group when the custom connector has a `log_destination` and discards it
otherwise. The runtime role is the task role; the Batch execution role, which pulls the images, is
never given to a container. The job ID isn't in the definition: the job state machine sets it on
both containers when it submits the job, and Fluent Bit expands `${...}` in the FireLens options,
so each job writes its own log stream.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from ccf_api_models.base import ValueModel
from ccf_api_models.contracts import RuntimeRoleTagsContract
from ccf_api_models.job_container import CONNECTOR_CONTAINER, LOG_ROUTER_CONTAINER, RuntimeVariable

from common.settings import JobDefinitionSettings
from common.storage.ddb.custom_connectors_dao import ConnectorItem

if TYPE_CHECKING:
    from mypy_boto3_batch.client import BatchClient
    from mypy_boto3_batch.type_defs import (
        EcsPropertiesTypeDef,
        KeyValuePairTypeDef,
        LogConfigurationTypeDef,
        TaskContainerPropertiesTypeDef,
    )

LOG_ROUTER_CPU = Decimal("0.25")
LOG_ROUTER_MEMORY_MIB = 512
_INACTIVE = "INACTIVE"


class RegisterJobDefinitionRequest(ValueModel):
    """The custom connector whose job definition is registered, as it is stored."""

    connector: ConnectorItem


class ConnectorJobDefinitions:
    """Registers and deregisters one custom connector's Batch job definition. A lifecycle manager: two methods."""

    def __init__(self, batch_client: BatchClient, settings: JobDefinitionSettings) -> None:
        self._batch = batch_client
        self._settings = settings
        self._region = batch_client.meta.region_name

    def register(self, request: RegisterJobDefinitionRequest) -> str:
        """Register a new revision named `<name_prefix><destination_id>-<connector_id>` and return its ARN."""
        connector = request.connector
        tags = RuntimeRoleTagsContract.load()
        response = self._batch.register_job_definition(
            jobDefinitionName=f"{self._settings.name_prefix}{connector.destination_id}-{connector.connector_id}",
            type="container",
            platformCapabilities=["FARGATE"],
            ecsProperties=self._ecs_properties(connector),
            timeout={"attemptDurationSeconds": connector.container_properties.timeout},
            tags={tags.destination_id: connector.destination_id, tags.connector_id: connector.connector_id},
            propagateTags=True,
        )
        return response["jobDefinitionArn"]

    def deregister(self, job_definition_arn: str) -> None:
        """Deregister the revision; one Batch reports missing or `INACTIVE` is already done."""
        described = self._batch.describe_job_definitions(jobDefinitions=[job_definition_arn])["jobDefinitions"]
        if not described or described[0].get("status") == _INACTIVE:
            return
        self._batch.deregister_job_definition(jobDefinition=job_definition_arn)

    def _ecs_properties(self, connector: ConnectorItem) -> EcsPropertiesTypeDef:
        return {
            "taskProperties": [
                {
                    "executionRoleArn": self._settings.execution_role_arn,
                    "taskRoleArn": connector.runtime_role_arn,
                    "platformVersion": "LATEST",
                    "runtimePlatform": {"operatingSystemFamily": "LINUX", "cpuArchitecture": "ARM64"},
                    "networkConfiguration": {"assignPublicIp": "DISABLED"},
                    "containers": [self._connector(connector), self._log_router()],
                }
            ]
        }

    def _connector(self, connector: ConnectorItem) -> TaskContainerPropertiesTypeDef:
        container = connector.container_properties
        cpu = Decimal(str(container.resource_requirements.cpu)) - LOG_ROUTER_CPU
        memory = container.resource_requirements.memory - LOG_ROUTER_MEMORY_MIB
        return {
            "name": CONNECTOR_CONTAINER,
            "essential": True,
            "image": container.image_uri,
            "command": list(container.command),
            "environment": self._environment(connector),
            "dependsOn": [{"containerName": LOG_ROUTER_CONTAINER, "condition": "START"}],
            "resourceRequirements": [
                {"type": "VCPU", "value": format(cpu.normalize(), "f")},
                {"type": "MEMORY", "value": str(memory)},
            ],
            "logConfiguration": self._firelens(connector),
        }

    def _environment(self, connector: ConnectorItem) -> list[KeyValuePairTypeDef]:
        values = {
            RuntimeVariable.API_ENDPOINT: self._settings.api_endpoint,
            RuntimeVariable.REGION: self._region,
            RuntimeVariable.TENANT_ARN_PREFIX: connector.tenant_arn_prefix,
            RuntimeVariable.DESTINATION_ID: connector.destination_id,
            RuntimeVariable.CONNECTOR_ID: connector.connector_id,
        }
        return [{"name": name.value, "value": value} for name, value in values.items()]

    def _firelens(self, connector: ConnectorItem) -> LogConfigurationTypeDef:
        destination = connector.log_destination
        if destination is None:
            return {"logDriver": "awsfirelens", "options": {"Name": "null"}}
        return {
            "logDriver": "awsfirelens",
            "options": {
                "Name": "cloudwatch_logs",
                "region": self._region,
                "log_group_name": str(connector.log_group_name),
                "log_stream_name": f"${{{RuntimeVariable.JOB_ID.value}}}",
                "log_key": "log",
                "log_format": "json/emf",
                "auto_create_group": "false",
                "role_arn": destination.role_arn,
                "external_id": connector.source_access.external_id,
            },
        }

    def _log_router(self) -> TaskContainerPropertiesTypeDef:
        return {
            "name": LOG_ROUTER_CONTAINER,
            "essential": True,
            "image": self._settings.log_router_image,
            "firelensConfiguration": {"type": "fluentbit", "options": {"enable-ecs-log-metadata": "false"}},
            "resourceRequirements": [
                {"type": "VCPU", "value": format(LOG_ROUTER_CPU, "f")},
                {"type": "MEMORY", "value": str(LOG_ROUTER_MEMORY_MIB)},
            ],
            "logConfiguration": {
                "logDriver": "awslogs",
                "options": {
                    "awslogs-group": self._settings.log_router_log_group,
                    "awslogs-region": self._region,
                    "awslogs-stream-prefix": self._settings.log_router_stream_prefix,
                },
            },
        }
