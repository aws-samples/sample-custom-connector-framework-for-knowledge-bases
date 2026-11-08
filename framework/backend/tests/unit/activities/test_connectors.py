"""Custom connector operations, with IAM, Scheduler, Step Functions, and the codec stubbed."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError
from ccf_api_models.catalog import OperationCatalog
from ccf_api_models.identifiers import ConnectorRef, DestinationRef, JobRef, Partition, TenantContext
from ccf_api_models.models import (
    ConnectorSchedule,
    ContainerProperties,
    CreateCustomConnectorRequest,
    CreateDestinationRequest,
    DeleteCustomConnectorRequest,
    DestinationType,
    GetCustomConnectorRequest,
    ListCustomConnectorsRequest,
    ListCustomConnectorsResponseContent,
    LogDestination,
    ResourceRequirements,
    ScheduleState,
    SourceAccessInput,
    UpdateContainerProperties,
    UpdateCustomConnectorRequest,
)
from ccf_api_models.operations import Operation
from pydantic import SecretStr

from activities.connectors import (
    CreateCustomConnectorActivity,
    DeleteCustomConnectorActivity,
    GetCustomConnectorActivity,
    UpdateCustomConnectorActivity,
)
from common.connector_job_definitions import ConnectorJobDefinitions
from common.errors import BadRequestError, DependencyUnavailableError, ResourceConflictError, ResourceNotFoundError
from common.external_ids import ExternalIdGenerator
from common.operation import OperationRequest, RequestTrace
from common.read_resources import ListResources
from common.resource_data_codec import ResourceDataCodec
from common.resource_ids import ResourceIds, SystemClock
from common.runtime_role import ConnectorRuntimeRoleLifecycle
from common.schedule_lifecycle import ScheduleLifecycle
from common.settings import RuntimeRoleSettings, SchedulerSettings
from common.storage.ddb.custom_connector_jobs_dao import CustomConnectorJobsDao, JobsTables, StartJobRecord
from common.storage.ddb.custom_connectors_dao import CustomConnectorsDao, CustomConnectorsTables
from common.storage.ddb.destinations_dao import CreateDestinationRecord, DestinationsDao, DestinationsTable
from common.validators.connector_request_validator import ConnectorRequestValidator
from common.workflow_executions import WorkflowExecutions
from tests.unit.common.test_connector_job_definitions import SETTINGS as JOB_DEFINITION_SETTINGS
from tests.unit.common.test_connector_job_definitions import FakeBatch
from tests.unit.conftest import DynamoDbFailures, FailingDynamoDbCall
from tests.unit.dao_world import JOB_SETTINGS, ConsistentReadSpy

TENANT = TenantContext(partition=Partition.AWS, region="us-east-1", account_id="111122223333")
RUNTIME_ROLE_NAMES = {
    "role_path": "/ccf-runtime/",
    "role_name_prefix": "CcfConnectorRuntime-",
    "policy_name": "CcfConnectorRuntimePolicy",
    "max_session_seconds": 43_200,
}
SERVICE_ACCOUNT = "444455556666"
SERVICE = TenantContext(partition=Partition.AWS, region="us-east-1", account_id=SERVICE_ACCOUNT)
DESTINATION = DestinationRef(tenant=TENANT, destination_id="dst-7c41e9a2b5d0")
OTHER_DESTINATION = DestinationRef(tenant=TENANT, destination_id="dst-0a1b2c3d4e5f")
IMAGE_URI = "111122223333.dkr.ecr.us-east-1.amazonaws.com/ccf-connectors/github:abc"
SOURCE_ROLE_ARN = "arn:aws:iam::111122223333:role/CcfSourceRole-github-awslabs"
LOG_GROUP_ARN = "arn:aws:logs:us-east-1:111122223333:log-group:/ccf/connectors/github-awslabs"
LOG_ROLE_ARN = "arn:aws:iam::111122223333:role/CcfLogDeliveryRole-github-awslabs"
DESTINATION_ROLE_ARN = "arn:aws:iam::111122223333:role/ccf/destinations/CcfDestinationRole-docs"
API_REQUEST_ID = "c8f9c4b5-0000-4000-8000-000000000000"
DELETION_WORKFLOW_ARN = "arn:aws:states:us-east-1:444455556666:stateMachine:CcfConnectorDeletionWorkflow"
OPENAPI = Path(__file__).resolve().parents[4] / "api/generated/openapi.json"


def runtime_routes_json() -> str:
    """The runtime-callable routes as `api-service.ts` writes them: one per operation whose callers hold RUNTIME."""
    document = json.loads(OPENAPI.read_text())
    routes = [
        {"method": method.upper(), "path": path}
        for path, operations in document["paths"].items()
        for method, operation in operations.items()
        if "RUNTIME" in operation["x-ccf-callers"]
    ]
    return json.dumps(routes)


@dataclass
class FakeIam:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    fail: set[str] = field(default_factory=set)

    def _record(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, arguments))
        if name in self.fail:
            raise ClientError({"Error": {"Code": "ServiceFailure", "Message": "no"}}, name)
        return {}

    def create_role(self, **arguments: Any) -> dict[str, Any]:
        return self._record("CreateRole", arguments)

    def tag_role(self, **arguments: Any) -> dict[str, Any]:
        return self._record("TagRole", arguments)

    def untag_role(self, **arguments: Any) -> dict[str, Any]:
        return self._record("UntagRole", arguments)

    def put_role_policy(self, **arguments: Any) -> dict[str, Any]:
        return self._record("PutRolePolicy", arguments)

    def delete_role_policy(self, **arguments: Any) -> dict[str, Any]:
        return self._record("DeleteRolePolicy", arguments)

    def delete_role(self, **arguments: Any) -> dict[str, Any]:
        return self._record("DeleteRole", arguments)

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def last(self, name: str) -> dict[str, Any]:
        return [arguments for called, arguments in self.calls if called == name][-1]


@dataclass
class FakeScheduler:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    fail: set[str] = field(default_factory=set)

    def _record(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, arguments))
        if name in self.fail:
            raise ClientError({"Error": {"Code": "InternalServerException", "Message": "no"}}, name)
        return {}

    def create_schedule(self, **arguments: Any) -> dict[str, Any]:
        return self._record("CreateSchedule", arguments)

    def update_schedule(self, **arguments: Any) -> dict[str, Any]:
        return self._record("UpdateSchedule", arguments)

    def delete_schedule(self, **arguments: Any) -> dict[str, Any]:
        return self._record("DeleteSchedule", arguments)


@dataclass
class FakeStepFunctions:
    started: list[dict[str, Any]] = field(default_factory=list)
    statuses: dict[str, str] = field(default_factory=dict)
    fail_next_start: bool = False

    def start_execution(self, **arguments: Any) -> dict[str, Any]:
        if self.fail_next_start:
            self.fail_next_start = False
            raise ClientError({"Error": {"Code": "ServiceUnavailable", "Message": "down"}}, "StartExecution")
        self.started.append(arguments)
        name = arguments["name"]
        return {"executionArn": f"arn:aws:states:us-east-1:444455556666:execution:CcfConnectorDeletionWorkflow:{name}"}

    def describe_execution(self, executionArn: str) -> dict[str, Any]:  # noqa: N803
        return {"status": self.statuses.get(executionArn, "SUCCEEDED")}


TRACE = RequestTrace(correlation_id="3f6c2d1e-8b4a-4c7e-9f10-2a5b6c7d8e9f", api_request_id=API_REQUEST_ID)


class World:
    def __init__(self, resource: Any, names: Any, codec: ResourceDataCodec) -> None:
        self.resource = resource
        self.names = names
        self.destinations = DestinationsDao.over(
            DestinationsTable(
                table=resource.Table(names.destinations), client=resource.meta.client, clock=SystemClock()
            )
        )
        self.connectors = CustomConnectorsDao.over(
            CustomConnectorsTables(
                table=resource.Table(names.connectors),
                destinations_table=resource.Table(names.destinations),
                client=resource.meta.client,
                clock=SystemClock(),
            )
        )
        self.jobs = CustomConnectorJobsDao.over(
            JobsTables(
                jobs_table=resource.Table(names.jobs),
                connectors_table=resource.Table(names.connectors),
                destinations_table=resource.Table(names.destinations),
                client=resource.meta.client,
                settings=JOB_SETTINGS,
                clock=SystemClock(),
            )
        )
        self.iam = FakeIam()
        self.scheduler = FakeScheduler()
        self.sfn = FakeStepFunctions()
        self.runtime_roles = ConnectorRuntimeRoleLifecycle(
            self.iam,
            RuntimeRoleSettings.model_validate(
                {
                    "rest_api_id": "abcdef1234",
                    "stage_name": "ccf",
                    "boundary_arn": "arn:aws:iam::444455556666:policy/ccf/CcfConnectorRuntimeBoundary",
                    "routes": runtime_routes_json(),
                    **RUNTIME_ROLE_NAMES,
                }
            ),
            SERVICE,
        )
        self.schedules = ScheduleLifecycle(
            self.scheduler,
            SchedulerSettings(
                target_arn="arn:aws:lambda:us-east-1:444455556666:function:CcfScheduledStart",
                role_arn="arn:aws:iam::444455556666:role/ccf/CcfSchedulerRole",
                group_name="ccf-connectors",
                dead_letter_arn="arn:aws:sqs:us-east-1:444455556666:CcfSchedulerDlq",
                retry_attempts=3,
                max_event_age_seconds=3600,
            ),
        )
        validator = ConnectorRequestValidator("us-east-1", "amazonaws.com")
        self.batch = FakeBatch()
        job_definitions = ConnectorJobDefinitions(self.batch, JOB_DEFINITION_SETTINGS)  # type: ignore[arg-type]
        self.dynamodb_failures = DynamoDbFailures(resource)
        self.codec = codec
        self.create_activity = CreateCustomConnectorActivity(
            self.connectors,
            self.destinations,
            validator,
            codec,
            self.runtime_roles,
            self.schedules,
            job_definitions,
            ExternalIdGenerator(),
            ResourceIds(),
        )
        self.get_activity = GetCustomConnectorActivity(self.connectors, codec)
        self.list_activity = ListResources(
            self.connectors.list,
            DestinationRef.of,
            OperationCatalog.load().spec(Operation.LIST_CUSTOM_CONNECTORS),
            self.destinations.exists,
        )
        self.update_activity = UpdateCustomConnectorActivity(
            self.connectors, validator, codec, self.runtime_roles, self.schedules, job_definitions
        )
        self.delete_activity = DeleteCustomConnectorActivity(
            self.connectors,
            self.destinations,
            WorkflowExecutions(self.sfn, DELETION_WORKFLOW_ARN),  # type: ignore[arg-type]
            self.runtime_roles,
        )

    def fail_connector_write(self, operation: str) -> None:
        self.dynamodb_failures.fail(FailingDynamoDbCall(operation=operation, table_name=self.names.connectors))

    def destination(self, destination: DestinationRef = DESTINATION, status: str = "ACTIVE") -> None:
        payload = CreateDestinationRequest(name="docs", type=DestinationType.BEDROCK, role_arn=DESTINATION_ROLE_ARN)
        self.destinations.create(CreateDestinationRecord(destination=destination, payload=payload, correlation_id="c"))
        self.resource.Table(self.names.destinations).update_item(
            Key={"tenant_arn_prefix": TENANT.arn_prefix, "destination_id": destination.destination_id},
            UpdateExpression="SET #status = :status, destination_resources = :resources",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":status": status,
                ":resources": {
                    "bucket_arn": f"arn:aws:s3:::ccf-{destination.destination_id}-111122223333-us-east-1",
                    "bucket_name": f"ccf-{destination.destination_id}-111122223333-us-east-1",
                },
            },
        )

    def create(self, **overrides: Any) -> Any:
        return self.create_activity.create(
            OperationRequest(tenant=TENANT, payload=create_request(**overrides), trace=TRACE)
        )

    def get(self, connector_id: str, destination: DestinationRef = DESTINATION) -> Any:
        payload = GetCustomConnectorRequest(destination_id=destination.destination_id, connector_id=connector_id)
        return self.get_activity.fetch(OperationRequest(tenant=TENANT, payload=payload, trace=TRACE)).connector

    def update(self, connector_id: str, **members: Any) -> Any:
        payload = UpdateCustomConnectorRequest(
            destination_id=DESTINATION.destination_id, connector_id=connector_id, **members
        )
        return self.update_activity.update(OperationRequest(tenant=TENANT, payload=payload, trace=TRACE))

    def delete(self, connector_id: str, destination: DestinationRef = DESTINATION) -> None:
        payload = DeleteCustomConnectorRequest(destination_id=destination.destination_id, connector_id=connector_id)
        self.delete_activity.delete(OperationRequest(tenant=TENANT, payload=payload, trace=TRACE))

    def item(self, connector_id: str) -> dict[str, Any]:
        key = {
            "tenant_arn_prefix": TENANT.arn_prefix,
            "destination_connector_key": f"dst-7c41e9a2b5d0#{connector_id}",
        }
        item: dict[str, Any] = self.resource.Table(self.names.connectors).get_item(Key=key)["Item"]
        return item

    def start_job(self, connector_id: str) -> None:
        job = JobRef(
            connector=ConnectorRef(destination=DESTINATION, connector_id=connector_id), job_id="ccj-0fdef5d64630"
        )
        self.jobs.start_job(StartJobRecord(job=job))


def create_request(**overrides: Any) -> CreateCustomConnectorRequest:
    values: dict[str, Any] = {
        "destination_id": DESTINATION.destination_id,
        "name": "github-awslabs",
        "container_properties": ContainerProperties(
            image_uri=IMAGE_URI,
            command=["ccf-connector", "run", "connector:GitHubConnectorImplementation"],
            resource_requirements=ResourceRequirements(cpu=1.0, memory=2048),
            timeout=3600,
        ),
        "source_access": SourceAccessInput(role_arn=SOURCE_ROLE_ARN),
        **overrides,
    }
    return CreateCustomConnectorRequest(**values)


def log_destination(**overrides: str) -> LogDestination:
    return LogDestination.model_validate_json(
        json.dumps({"type": "CLOUDWATCH_LOGS", "arn": LOG_GROUP_ARN, "role_arn": LOG_ROLE_ARN, **overrides})
    )


@pytest.fixture
def world(dynamodb: Any, table_names: Any, codec: ResourceDataCodec) -> World:
    built = World(dynamodb, table_names, codec)
    built.destination()
    return built


def policy(world: World) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(world.iam.last("PutRolePolicy")["PolicyDocument"])
    return document


def tags(arguments: dict[str, Any]) -> dict[str, str]:
    return {tag["Key"]: tag["Value"] for tag in arguments["Tags"]}


# Create


def test_create_with_a_destination_that_is_not_active_conflicts_and_creates_no_role_or_schedule(
    dynamodb: Any, table_names: Any, codec: ResourceDataCodec
) -> None:
    world = World(dynamodb, table_names, codec)
    world.destination(status="CREATING_BUCKET")

    with pytest.raises(ResourceConflictError):
        world.create(schedule=ConnectorSchedule(expression="rate(7 days)", state=ScheduleState.ENABLED))

    assert world.iam.calls == []
    assert world.scheduler.calls == []


def test_create_in_a_missing_destination_is_not_found(world: World) -> None:
    with pytest.raises(ResourceNotFoundError):
        world.create(destination_id=OTHER_DESTINATION.destination_id)


def test_a_failed_transaction_deletes_the_runtime_role_job_definition_and_schedule(world: World) -> None:
    world.fail_connector_write("TransactWriteItems")

    with pytest.raises(DependencyUnavailableError):
        world.create(schedule=ConnectorSchedule(expression="rate(7 days)", state=ScheduleState.ENABLED))

    assert world.iam.names()[-2:] == ["DeleteRolePolicy", "DeleteRole"]
    assert world.scheduler.calls[-1][0] == "DeleteSchedule"
    assert len(world.batch.deregistered) == 1


def test_create_registers_the_custom_connectors_job_definition_and_stores_its_arn(world: World) -> None:
    connector_id = world.create().connector.connector_id

    [registered] = world.batch.registered
    assert registered["jobDefinitionName"] == f"ccf-{DESTINATION.destination_id}-{connector_id}"
    assert world.item(connector_id)["job_definition_arn"].endswith(":3")
    assert world.batch.deregistered == []


def test_an_update_of_the_container_registers_a_new_revision_and_then_deregisters_the_old_one(
    world: World,
) -> None:
    connector_id = world.create().connector.connector_id
    first = world.item(connector_id)["job_definition_arn"]

    world.update(connector_id, description="no definition change")
    world.update(connector_id, container_properties=UpdateContainerProperties(timeout=7200))

    assert [call["timeout"] for call in world.batch.registered] == [
        {"attemptDurationSeconds": 3600},
        {"attemptDurationSeconds": 7200},
    ]
    assert world.item(connector_id)["job_definition_arn"].endswith(":4")
    assert world.batch.deregistered == [first]


def test_a_failed_update_deregisters_the_revision_it_registered_and_keeps_the_stored_one(world: World) -> None:
    connector_id = world.create().connector.connector_id
    first = world.item(connector_id)["job_definition_arn"]
    world.fail_connector_write("UpdateItem")

    with pytest.raises(DependencyUnavailableError):
        world.update(connector_id, container_properties=UpdateContainerProperties(timeout=7200))

    assert len(world.batch.registered) == 2
    assert world.batch.deregistered == [first.replace(":3", ":4")]
    assert world.item(connector_id)["job_definition_arn"] == first


def test_create_whose_schedule_cleanup_fails_still_deletes_the_role_and_raises_the_original_error(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    world.fail_connector_write("TransactWriteItems")
    world.scheduler.fail.add("DeleteSchedule")

    with pytest.raises(DependencyUnavailableError):
        world.create(schedule=ConnectorSchedule(expression="rate(7 days)", state=ScheduleState.ENABLED))

    assert world.iam.names()[-2:] == ["DeleteRolePolicy", "DeleteRole"]
    [failed] = [record for record in caplog.records if record.getMessage() == "connector_compensation_failed"]
    assert (failed.levelname, failed.step) == ("ERROR", "schedule_delete")  # type: ignore[attr-defined]


def test_update_whose_schedule_restore_fails_still_restores_the_role_and_raises_the_original_error(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    connector_id = world.create().connector.connector_id
    world.fail_connector_write("UpdateItem")
    world.scheduler.fail.add("DeleteSchedule")

    with pytest.raises(DependencyUnavailableError):
        world.update(
            connector_id,
            log_destination=log_destination(),
            schedule=ConnectorSchedule(expression="rate(1 day)", state=ScheduleState.ENABLED),
        )

    assert policy(world)["Statement"][1]["Resource"] == [SOURCE_ROLE_ARN]
    [failed] = [record for record in caplog.records if record.getMessage() == "connector_compensation_failed"]
    assert failed.step == "schedule_restore"  # type: ignore[attr-defined]


def test_the_external_id_is_generated_and_returned_by_create_and_get(world: World) -> None:
    created = world.create().connector

    fetched = world.get(created.connector_id)

    assert re.fullmatch(r"ccf-ext-[0-9a-f]{32}", created.source_access.external_id)
    assert fetched.source_access.external_id == created.source_access.external_id


def test_runtime_role_arn_is_the_created_roles_arn_in_create_and_get(world: World) -> None:
    created = world.create().connector

    role_name = world.iam.last("CreateRole")["RoleName"]
    expected = f"arn:aws:iam::444455556666:role/ccf-runtime/{role_name}"
    assert created.runtime_role_arn == expected
    assert world.get(created.connector_id).runtime_role_arn == expected


def test_an_image_in_the_callers_ccf_connectors_repository_passes(world: World) -> None:
    created = world.create().connector

    assert world.get(created.connector_id).container_properties.image_uri == IMAGE_URI


@pytest.mark.parametrize("configuration", ['{"token": "x"}', "not json at all"])
def test_configuration_is_stored_and_returned_unchanged_by_create_and_update(world: World, configuration: str) -> None:
    connector_id = world.create(connector_configuration=SecretStr(configuration)).connector.connector_id
    assert world.get(connector_id).connector_configuration.get_secret_value() == configuration

    world.update(connector_id, connector_configuration=SecretStr(f"{configuration} again"))

    assert world.get(connector_id).connector_configuration.get_secret_value() == f"{configuration} again"


def test_a_create_the_validator_rejects_creates_no_runtime_role(world: World) -> None:
    too_large = "€" * 21_845 + "aa"

    with pytest.raises(BadRequestError) as raised:
        world.create(connector_configuration=SecretStr(too_large))

    assert raised.value.fields == ("connector_configuration",)
    assert world.iam.calls == []


def test_runtime_role_policy_and_tags_without_a_log_destination(world: World) -> None:
    created = world.create().connector
    document = policy(world)

    assert [statement["Sid"] for statement in document["Statement"]] == ["ConnectorApiRoutes", "TenantRoles"]
    routes, tenant_roles = document["Statement"]
    assert routes["Action"] == "execute-api:Invoke"
    assert tenant_roles == {
        "Sid": "TenantRoles",
        "Effect": "Allow",
        "Action": "sts:AssumeRole",
        "Resource": [SOURCE_ROLE_ARN],
    }
    actions = json.dumps([statement["Action"] for statement in document["Statement"]])
    assert not re.search(r"\b(s3|kms|iam):", actions)
    assert tags(world.iam.last("CreateRole")) == {
        "ccf:destination-id": DESTINATION.destination_id,
        "ccf:connector-id": created.connector_id,
        "ccf:tenant-account": "111122223333",
        "ccf:source-access-role": "CcfSourceRole-github-awslabs",
    }


def test_runtime_role_routes_name_the_connectors_own_ids(world: World) -> None:
    connector_id = world.create().connector.connector_id
    resources = policy(world)["Statement"][0]["Resource"]
    prefix = "arn:aws:execute-api:us-east-1:444455556666:abcdef1234/ccf/"

    assert len(resources) == len(json.loads(runtime_routes_json()))
    assert f"{prefix}GET/api/v1/destinations/{DESTINATION.destination_id}" in resources
    for resource in resources:
        assert resource.startswith(f"{prefix}") and f"/destinations/{DESTINATION.destination_id}" in resource
        if resource != f"{prefix}GET/api/v1/destinations/{DESTINATION.destination_id}":
            assert f"/custom-connectors/{connector_id}" in resource
        assert "/destinations/*" not in resource and "/custom-connectors/*" not in resource


def test_a_log_destination_adds_the_log_delivery_role_to_the_policy_and_tags(world: World) -> None:
    world.create(log_destination=log_destination())

    assert policy(world)["Statement"][1]["Resource"] == [SOURCE_ROLE_ARN, LOG_ROLE_ARN]
    assert tags(world.iam.last("CreateRole"))["ccf:log-delivery-role"] == "CcfLogDeliveryRole-github-awslabs"


def test_get_returns_the_stored_log_destination_without_the_wildcard(world: World) -> None:
    connector_id = world.create(log_destination=log_destination(arn=f"{LOG_GROUP_ARN}:*")).connector.connector_id

    assert world.get(connector_id).log_destination == log_destination()


# Update


def test_an_update_that_sends_source_access_or_a_log_destination_rewrites_tags_and_policy(world: World) -> None:
    connector_id = world.create().connector.connector_id

    world.update(connector_id, log_destination=log_destination())

    assert tags(world.iam.last("TagRole"))["ccf:log-delivery-role"] == "CcfLogDeliveryRole-github-awslabs"
    new_source = "arn:aws:iam::111122223333:role/CcfSourceRole-github-aws"

    world.update(connector_id, source_access=SourceAccessInput(role_arn=new_source))

    assert policy(world)["Statement"][1]["Resource"] == [new_source, LOG_ROLE_ARN]
    assert world.get(connector_id).source_access.role_arn == new_source


def test_an_update_that_sends_the_stored_source_access_leaves_the_runtime_role_alone(world: World) -> None:
    connector_id = world.create().connector.connector_id
    world.iam.calls.clear()

    world.update(connector_id, source_access=SourceAccessInput(role_arn=SOURCE_ROLE_ARN))

    assert world.iam.calls == []


def test_a_failed_update_restores_the_stored_tags_policy_and_schedule(world: World) -> None:
    connector_id = world.create().connector.connector_id
    world.fail_connector_write("UpdateItem")

    with pytest.raises(DependencyUnavailableError):
        world.update(
            connector_id,
            log_destination=log_destination(),
            schedule=ConnectorSchedule(expression="rate(1 day)", state=ScheduleState.ENABLED),
        )

    assert policy(world)["Statement"][1]["Resource"] == [SOURCE_ROLE_ARN]
    assert "ccf:log-delivery-role" not in tags(world.iam.last("TagRole"))
    assert world.iam.last("UntagRole")["TagKeys"] == ["ccf:log-delivery-role"]
    assert world.scheduler.calls[-1][0] == "DeleteSchedule"


DAILY = ConnectorSchedule(expression="rate(1 day)", state=ScheduleState.ENABLED)


def test_an_update_that_sends_a_null_log_destination_clears_it_and_the_runtime_roles_access(world: World) -> None:
    connector_id = world.create(log_destination=log_destination()).connector.connector_id
    world.iam.calls.clear()

    world.update(connector_id, log_destination=None)

    assert world.get(connector_id).log_destination is None
    assert "log_destination" not in world.item(connector_id)
    assert world.iam.last("UntagRole")["TagKeys"] == ["ccf:log-delivery-role"]
    assert policy(world)["Statement"][1]["Resource"] == [SOURCE_ROLE_ARN]


def test_an_update_that_sends_a_null_schedule_deletes_the_schedule(world: World) -> None:
    connector_id = world.create(schedule=DAILY).connector.connector_id
    world.scheduler.calls.clear()

    world.update(connector_id, schedule=None)

    assert [name for name, _ in world.scheduler.calls] == ["DeleteSchedule"]
    assert world.get(connector_id).schedule is None
    assert "schedule" not in world.item(connector_id)


def test_an_update_that_leaves_out_the_log_destination_and_schedule_keeps_both(world: World) -> None:
    connector_id = world.create(log_destination=log_destination(), schedule=DAILY).connector.connector_id
    world.scheduler.calls.clear()
    world.iam.calls.clear()

    world.update(connector_id, description="Reads awslabs")

    assert world.get(connector_id).log_destination == log_destination()
    assert world.get(connector_id).schedule == DAILY
    assert world.scheduler.calls == []
    assert world.iam.calls == []


def test_a_failed_update_that_cleared_the_schedule_puts_the_stored_schedule_back(world: World) -> None:
    connector_id = world.create(schedule=DAILY).connector.connector_id
    world.fail_connector_write("UpdateItem")
    world.scheduler.calls.clear()

    with pytest.raises(DependencyUnavailableError):
        world.update(connector_id, schedule=None)

    assert [name for name, _ in world.scheduler.calls] == ["DeleteSchedule", "UpdateSchedule"]


def test_update_while_a_job_runs_conflicts(world: World) -> None:
    connector_id = world.create().connector.connector_id
    world.start_job(connector_id)

    with pytest.raises(ResourceConflictError):
        world.update(connector_id, description="new")


# Get and list


def test_a_runtime_roles_get_reuses_the_resolvers_custom_connector_item(world: World) -> None:
    connector_id = world.create().connector.connector_id
    stored = world.connectors.get(ConnectorRef(destination=DESTINATION, connector_id=connector_id))
    spy = ConsistentReadSpy(world.resource.Table(world.names.connectors))
    spied = CustomConnectorsDao.over(
        CustomConnectorsTables(
            table=spy,
            destinations_table=world.resource.Table(world.names.destinations),
            client=world.resource.meta.client,
            clock=SystemClock(),
        )
    )
    payload = GetCustomConnectorRequest(destination_id=DESTINATION.destination_id, connector_id=connector_id)

    fetched = GetCustomConnectorActivity(spied, world.codec).fetch(
        OperationRequest(tenant=TENANT, payload=payload, trace=TRACE, connector=stored)
    )

    assert fetched.connector.connector_id == connector_id
    assert spy.reads == []


def test_a_custom_connector_in_another_destination_is_not_found(world: World) -> None:
    world.destination(OTHER_DESTINATION)
    connector_id = world.create().connector.connector_id

    with pytest.raises(ResourceNotFoundError):
        world.get(connector_id, OTHER_DESTINATION)
    with pytest.raises(ResourceNotFoundError):
        world.delete(connector_id, OTHER_DESTINATION)


def test_list_returns_the_destinations_custom_connectors(world: World) -> None:
    world.create()
    payload = ListCustomConnectorsRequest(destination_id=DESTINATION.destination_id)

    listed = ListCustomConnectorsResponseContent.model_validate(
        world.list_activity.list(OperationRequest(tenant=TENANT, payload=payload, trace=TRACE)), from_attributes=True
    )

    assert [summary.name for summary in listed.connectors] == ["github-awslabs"]


def test_list_in_a_missing_destination_is_not_found(world: World) -> None:
    payload = ListCustomConnectorsRequest(destination_id=OTHER_DESTINATION.destination_id)

    with pytest.raises(ResourceNotFoundError):
        world.list_activity.list(OperationRequest(tenant=TENANT, payload=payload, trace=TRACE))


# Delete


def test_delete_while_a_job_runs_conflicts(world: World) -> None:
    connector_id = world.create().connector.connector_id
    world.start_job(connector_id)

    with pytest.raises(ResourceConflictError):
        world.delete(connector_id)

    assert world.sfn.started == []


def test_delete_in_a_destination_without_a_bucket_conflicts_and_leaves_the_connector_available(world: World) -> None:
    connector_id = world.create().connector.connector_id
    world.resource.Table(world.names.destinations).update_item(
        Key={"tenant_arn_prefix": TENANT.arn_prefix, "destination_id": DESTINATION.destination_id},
        UpdateExpression="REMOVE destination_resources.bucket_arn, destination_resources.bucket_name",
    )

    with pytest.raises(ResourceConflictError):
        world.delete(connector_id)

    item = world.item(connector_id)
    assert item["status"] == "AVAILABLE"
    assert "deletion_execution_arn" not in item
    assert world.sfn.started == []


def test_delete_sets_deleting_and_starts_the_workflow_with_design_inputs(world: World) -> None:
    connector_id = world.create().connector.connector_id
    runtime_role_name = world.iam.last("CreateRole")["RoleName"]

    world.delete(connector_id)

    [started] = world.sfn.started
    connector_arn = f"{DESTINATION.arn}/custom-connector/{connector_id}"
    assert started["name"] == f"{connector_id}-delete-c8f9c4b5"
    workflow_input = json.loads(started["input"])
    assert set(workflow_input) == {
        "tenant_arn_prefix",
        "destination_id",
        "connector_id",
        "connector_arn",
        "destination_connector_key",
        "destination_type",
        "bucket_name",
        "destination_role_arn",
        "runtime_role_name",
        "schedule_name",
        "job_definition_arn",
        "correlation_id",
        "api_request_id",
        "continuation",
    }
    assert workflow_input["continuation"] == 0
    assert workflow_input["destination_role_arn"] == DESTINATION_ROLE_ARN
    assert workflow_input["connector_arn"] == connector_arn
    assert workflow_input["destination_connector_key"] == f"{DESTINATION.destination_id}#{connector_id}"
    assert workflow_input["bucket_name"] == "ccf-dst-7c41e9a2b5d0-111122223333-us-east-1"
    assert workflow_input["destination_type"] == "BEDROCK"
    assert workflow_input["runtime_role_name"] == runtime_role_name
    assert re.fullmatch(r"ccf-[0-9a-f]{32}", workflow_input["schedule_name"])
    assert workflow_input["job_definition_arn"] == world.item(connector_id)["job_definition_arn"]
    item = world.item(connector_id)
    assert item["status"] == "DELETING"
    assert item["deletion_execution_arn"].endswith(started["name"])


def test_delete_whose_workflow_fails_to_start_stays_deleting_and_the_next_delete_starts_it(world: World) -> None:
    connector_id = world.create().connector.connector_id
    world.sfn.fail_next_start = True

    with pytest.raises(DependencyUnavailableError):
        world.delete(connector_id)

    assert world.item(connector_id)["status"] == "DELETING"
    assert "deletion_execution_arn" not in world.item(connector_id)
    world.delete(connector_id)
    assert len(world.sfn.started) == 1


@pytest.mark.parametrize(("recorded_status", "executions"), [("RUNNING", 1), ("FAILED", 2)])
def test_a_repeated_delete_starts_an_execution_only_when_the_recorded_one_isnt_running(
    world: World, recorded_status: str, executions: int
) -> None:
    connector_id = world.create().connector.connector_id
    world.delete(connector_id)
    world.sfn.statuses[world.item(connector_id)["deletion_execution_arn"]] = recorded_status

    world.delete(connector_id)

    assert len(world.sfn.started) == executions


OUTSIDERS = [
    pytest.param(
        TenantContext(partition=Partition.AWS, region="us-east-1", account_id="999988887777"),
        DESTINATION.destination_id,
        id="another-tenant",
    ),
    pytest.param(TENANT, OTHER_DESTINATION.destination_id, id="another-destination"),
]


def create_for(tenant: TenantContext, destination_id: str) -> CreateCustomConnectorRequest:
    """A create whose image and source-access role are in the caller's own account, so only the destination is wrong."""
    return create_request(
        destination_id=destination_id,
        container_properties=ContainerProperties(
            image_uri=IMAGE_URI.replace(TENANT.account_id, tenant.account_id),
            command=["ccf-connector"],
            resource_requirements=ResourceRequirements(cpu=1.0, memory=2048),
            timeout=3600,
        ),
        source_access=SourceAccessInput(role_arn=SOURCE_ROLE_ARN.replace(TENANT.account_id, tenant.account_id)),
    )


@pytest.mark.parametrize(("tenant", "destination_id"), OUTSIDERS)
def test_every_connector_operation_from_another_tenant_or_destination_is_not_found_and_writes_nothing(
    world: World, tenant: TenantContext, destination_id: str
) -> None:
    connector_id = world.create().connector.connector_id
    connectors_table = world.resource.Table(world.names.connectors)
    before = (connectors_table.scan()["Items"], world.iam.names(), len(world.sfn.started))
    ids = {"destination_id": destination_id, "connector_id": connector_id}
    operations: list[Callable[[], object]] = [
        lambda: world.create_activity.create(
            OperationRequest(tenant=tenant, payload=create_for(tenant, destination_id), trace=TRACE)
        ),
        lambda: world.get_activity.fetch(
            OperationRequest(tenant=tenant, payload=GetCustomConnectorRequest(**ids), trace=TRACE)
        ),
        lambda: world.list_activity.list(
            OperationRequest(
                tenant=tenant, payload=ListCustomConnectorsRequest(destination_id=destination_id), trace=TRACE
            )
        ),
        lambda: world.update_activity.update(
            OperationRequest(
                tenant=tenant, payload=UpdateCustomConnectorRequest(**ids, description="moved"), trace=TRACE
            )
        ),
        lambda: world.delete_activity.delete(
            OperationRequest(tenant=tenant, payload=DeleteCustomConnectorRequest(**ids), trace=TRACE)
        ),
    ]

    for operation in operations:
        with pytest.raises(ResourceNotFoundError):
            operation()

    assert (connectors_table.scan()["Items"], world.iam.names(), len(world.sfn.started)) == before
