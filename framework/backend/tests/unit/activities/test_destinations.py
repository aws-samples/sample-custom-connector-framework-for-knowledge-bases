"""Destination operations: create, get, list, and delete, with Step Functions stubbed at the AWS boundary."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from botocore.exceptions import ClientError
from ccf_api_models.base import ValueModel
from ccf_api_models.catalog import OperationCatalog
from ccf_api_models.identifiers import ConnectorRef, DestinationRef, Partition, TenantContext
from ccf_api_models.models import (
    ContainerProperties,
    CreateCustomConnectorRequest,
    CreateDestinationRequest,
    CreateDestinationResponseContent,
    DeleteDestinationRequest,
    DestinationStatus,
    DestinationType,
    GetDestinationRequest,
    ListDestinationsRequest,
    MetadataField,
    MetadataFieldSearchability,
    MetadataFieldType,
    ResourceRequirements,
    SourceAccessInput,
    UpdateDestinationRequest,
)
from ccf_api_models.operations import Operation
from pydantic import ValidationError

from activities.destinations import CreateDestinationActivity, DeleteDestinationActivity, UpdateDestinationActivity
from common.errors import BadRequestError, DependencyUnavailableError, ResourceConflictError, ResourceNotFoundError
from common.idempotency import IdempotentCreates, IdempotentRunRequest
from common.operation import OperationRequest, RequestTrace
from common.read_resources import ListResources, ReadResource
from common.resource_ids import ResourceIds, SystemClock
from common.settings import IdempotencySettings, TableNames
from common.storage.bedrock.knowledge_base_fields_dao import (
    KnowledgeBaseUpdatingError,
    MetadataFieldsRejectedError,
    SetMetadataFieldsRequest,
)
from common.storage.ddb.custom_connectors_dao import CreateConnectorRecord, CustomConnectorsDao, CustomConnectorsTables
from common.storage.ddb.destinations_dao import DestinationsDao, DestinationsTable
from common.validators.create_destination_validator import CreateDestinationValidator
from common.workflow_executions import WorkflowExecutions

CATALOG = OperationCatalog.load()
TENANT = TenantContext(partition=Partition.AWS, region="us-east-1", account_id="111122223333")
OTHER_TENANT = TenantContext(partition=Partition.AWS, region="us-east-1", account_id="444455556666")
ROLE_ARN = "arn:aws:iam::111122223333:role/ccf/destinations/CcfDestinationRole-docs"
BOUNDARY_ARN = "arn:aws:iam::111122223333:policy/ccf/destinations/CcfDestinationBoundary-docs"
API_REQUEST_ID = "c8f9c4b5-0000-4000-8000-000000000000"
CORRELATION_ID = "3f6c2d1e-8b4a-4c7e-9f10-2a5b6c7d8e9f"
STATE_MACHINE_ARN = "arn:aws:states:us-east-1:444455556666:stateMachine:CcfDestinationWorkflow"
KNOWLEDGE_BASE_ID = "KB12345678"


@dataclass
class FakeStepFunctions:
    """Records `StartExecution` calls, fails one on request, and reports each execution's status."""

    calls: list[str] = field(default_factory=list)
    started: list[dict[str, Any]] = field(default_factory=list)
    statuses: dict[str, str] = field(default_factory=dict)
    fail_next_start: bool = False

    def start_execution(self, **arguments: Any) -> dict[str, Any]:
        self.calls.append("StartExecution")
        if self.fail_next_start:
            self.fail_next_start = False
            raise ClientError({"Error": {"Code": "ServiceUnavailable", "Message": "down"}}, "StartExecution")
        if any(started["name"] == arguments["name"] for started in self.started):
            raise ClientError({"Error": {"Code": "ExecutionAlreadyExists", "Message": "dup"}}, "StartExecution")
        self.started.append(arguments)
        return {"executionArn": self.execution_arn(arguments["name"])}

    def describe_execution(self, executionArn: str) -> dict[str, Any]:  # noqa: N803
        return {"status": self.statuses.get(executionArn, "SUCCEEDED")}

    @staticmethod
    def execution_arn(name: str) -> str:
        return f"{STATE_MACHINE_ARN.replace(':stateMachine:', ':execution:')}:{name}"


class FailingCall(ValueModel):
    """A DynamoDB call on one table that answers with a server error, injected below the DAO."""

    operation: str
    table_name: str


@dataclass
class FakeKnowledgeBases:
    """Records each fields update and raises the error a test sets, as Bedrock would."""

    calls: list[SetMetadataFieldsRequest] = field(default_factory=list)
    error: Exception | None = None

    def set_fields(self, request: SetMetadataFieldsRequest) -> None:
        self.calls.append(request)
        if self.error is not None:
            raise self.error


class World:
    def __init__(self, resource: Any, names: TableNames) -> None:
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
        self.sfn = FakeStepFunctions()
        workflow = WorkflowExecutions(self.sfn, STATE_MACHINE_ARN)  # type: ignore[arg-type]
        self.trace = RequestTrace(correlation_id=CORRELATION_ID, api_request_id=API_REQUEST_ID)
        """Every call's trace; a test replaces it to model a later API call."""
        self.create_activity = CreateDestinationActivity(
            CreateDestinationValidator(), self.destinations, workflow, ResourceIds()
        )
        self.knowledge_bases = FakeKnowledgeBases()
        self.update_activity = UpdateDestinationActivity(self.destinations, self.knowledge_bases)  # type: ignore[arg-type]
        self.read = ReadResource(
            lambda destination: self.destinations.get(destination).destination(),
            DestinationRef.of,
            CATALOG.spec(Operation.GET_DESTINATION),
        )
        self.listing = ListResources(
            self.destinations.list, lambda request: request.tenant, CATALOG.spec(Operation.LIST_DESTINATIONS)
        )
        self.delete_activity = DeleteDestinationActivity(self.destinations, self.connectors, workflow)

    def request(self, payload: Any, tenant: TenantContext = TENANT) -> OperationRequest[Any]:
        return OperationRequest(tenant=tenant, payload=payload, trace=self.trace)

    def create(self, payload: CreateDestinationRequest) -> CreateDestinationResponseContent:
        return self.create_activity.create(self.request(payload))

    def get(self, destination_id: str, tenant: TenantContext = TENANT) -> Any:
        return self.read.read(self.request(GetDestinationRequest(destination_id=destination_id), tenant))

    def item(self, destination_id: str) -> dict[str, Any] | None:
        key = {"tenant_arn_prefix": TENANT.arn_prefix, "destination_id": destination_id}
        item: dict[str, Any] | None = self.resource.Table(self.names.destinations).get_item(Key=key).get("Item")
        return item

    def set_status(self, destination_id: str, status: DestinationStatus) -> None:
        self.resource.Table(self.names.destinations).update_item(
            Key={"tenant_arn_prefix": TENANT.arn_prefix, "destination_id": destination_id},
            UpdateExpression="SET #status = :status",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":status": status.value},
        )

    def record_last_execution(self, destination_id: str) -> str:
        """Write the last started execution's ARN, as the workflow's first state does."""
        arn = self.sfn.execution_arn(self.sfn.started[-1]["name"])
        self.resource.Table(self.names.destinations).update_item(
            Key={"tenant_arn_prefix": TENANT.arn_prefix, "destination_id": destination_id},
            UpdateExpression="SET workflow_execution_arn = :arn",
            ExpressionAttributeValues={":arn": arn},
        )
        return arn

    def fail(self, call: FailingCall) -> None:
        """Answer every matching call with a DynamoDB server error, the way the service does."""

        def failing(params: dict[str, Any], **_: Any) -> None:
            if params.get("TableName") == call.table_name:
                raise ClientError({"Error": {"Code": "InternalServerError", "Message": "injected"}}, call.operation)

        self.resource.meta.client.meta.events.register(f"provide-client-params.dynamodb.{call.operation}", failing)

    def active_destination(self) -> str:
        destination_id = self.create(request()).destination.destination_id
        self.set_status(destination_id, DestinationStatus.ACTIVE)
        return destination_id

    def add_connector(self, destination_id: str) -> None:
        connector = ConnectorRef(
            destination=DestinationRef(tenant=TENANT, destination_id=destination_id), connector_id="cc-e020d65d76c0"
        )
        payload = CreateCustomConnectorRequest(
            destination_id=destination_id,
            name="github-awslabs",
            container_properties=ContainerProperties(
                image_uri="111122223333.dkr.ecr.us-east-1.amazonaws.com/ccf-connectors/github:1.0.0",
                command=["ccf-connector"],
                resource_requirements=ResourceRequirements(cpu=1.0, memory=2048),
                timeout=3600,
            ),
            source_access=SourceAccessInput(role_arn="arn:aws:iam::111122223333:role/CcfSourceRole-github"),
        )
        self.connectors.create(
            CreateConnectorRecord(
                connector=connector,
                payload=payload,
                external_id="ccf-ext-0123456789abcdef0123456789abcdef",
                runtime_role_arn="arn:aws:iam::444455556666:role/ccf-runtime/CcfConnectorRuntime-3c9e1f0a2b7d4e6f8a1b2c3d4e5f6a7b",
            )
        )

    def delete(self, destination_id: str, tenant: TenantContext = TENANT) -> None:
        self.delete_activity.delete(self.request(DeleteDestinationRequest(destination_id=destination_id), tenant))

    def update(self, payload: UpdateDestinationRequest, tenant: TenantContext = TENANT) -> Any:
        return self.update_activity.update(self.request(payload, tenant))

    def set_knowledge_base(self, destination_id: str) -> None:
        """Record the knowledge base the create path made, as `SetActive` does."""
        self.resource.Table(self.names.destinations).update_item(
            Key={"tenant_arn_prefix": TENANT.arn_prefix, "destination_id": destination_id},
            UpdateExpression="SET destination_resources = :resources",
            ExpressionAttributeValues={":resources": {"knowledge_base_id": KNOWLEDGE_BASE_ID}},
        )

    def deletion_executions(self) -> list[dict[str, Any]]:
        return [call for call in self.sfn.started if "-delete-" in call["name"]]


class IdempotencyRoot(ValueModel):
    """The two settings `IdempotentCreates.from_settings` reads, over the test's `CcfIdempotency` table."""

    tables: TableNames
    idempotency: IdempotencySettings


def idempotent_creates(world: World) -> IdempotentCreates:
    """Idempotency over a new `CcfIdempotency` table, keyed as the CDK table is."""
    world.resource.create_table(
        TableName=world.names.idempotency,
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
    )
    return IdempotentCreates.from_settings(
        IdempotencyRoot(tables=world.names, idempotency=IdempotencySettings(expiry_seconds=3600))
    )


def request(**overrides: Any) -> CreateDestinationRequest:
    values: dict[str, Any] = {"name": "docs", "type": DestinationType.BEDROCK, "role_arn": ROLE_ARN, **overrides}
    return CreateDestinationRequest(**values)


@pytest.fixture
def world(dynamodb: Any, table_names: TableNames) -> World:
    return World(dynamodb, table_names)


def test_create_returns_a_random_destination_in_creating_bucket(world: World) -> None:
    created = world.create(request()).destination

    assert created.status is DestinationStatus.CREATING_BUCKET
    assert re.fullmatch(r"dst-[0-9a-f]{12}", created.destination_id)


def test_create_starts_one_execution_named_by_the_creation_time_in_milliseconds(world: World) -> None:
    created = world.create(request()).destination

    [started] = world.sfn.started
    created_at_ms = int(float(world.item(created.destination_id)["created_at"]) * 1000)  # type: ignore[index]
    assert started["name"] == f"{created.destination_id}-create-{created_at_ms}"
    assert re.fullmatch(rf"{created.destination_id}-create-[0-9]{{13}}", started["name"])


def test_create_sends_the_create_action_trace_and_execution_name_to_the_workflow(world: World) -> None:
    world.create(request())

    [started] = world.sfn.started
    workflow_input = json.loads(started["input"])
    assert workflow_input["action"] == "CREATE"
    assert (workflow_input["correlation_id"], workflow_input["api_request_id"]) == (CORRELATION_ID, API_REQUEST_ID)
    assert (workflow_input["execution_name"], workflow_input["continuation"]) == (started["name"], 0)


def test_create_leaves_recording_the_execution_to_the_workflow(world: World) -> None:
    created = world.create(request()).destination

    assert "workflow_execution_arn" not in world.item(created.destination_id)  # type: ignore[operator]


def test_create_passes_the_destination_roles_own_boundary_to_the_workflow(world: World) -> None:
    world.create(request())

    [started] = world.sfn.started
    workflow_input = json.loads(started["input"])
    assert (workflow_input["role_arn"], workflow_input["boundary_arn"]) == (ROLE_ARN, BOUNDARY_ARN)


def test_create_stores_returns_and_passes_access_control_on_bedrock(world: World) -> None:
    created = world.create(request(access_control_enabled=True)).destination

    assert created.access_control_enabled is True
    assert world.get(created.destination_id).destination.access_control_enabled is True
    assert json.loads(world.sfn.started[0]["input"])["access_control_enabled"] is True


@pytest.mark.parametrize("destination_type", [DestinationType.S3, DestinationType.QUICK])
def test_create_rejects_access_control_on_types_other_than_bedrock(
    world: World, destination_type: DestinationType
) -> None:
    with pytest.raises(BadRequestError) as raised:
        world.create(request(type=destination_type, access_control_enabled=True))

    assert raised.value.fields == ("access_control_enabled",)
    assert world.sfn.calls == []


def test_create_without_access_control_reads_back_false(world: World) -> None:
    created = world.create(request()).destination

    assert world.get(created.destination_id).destination.access_control_enabled is False


@pytest.mark.parametrize(
    "role_arn",
    [
        "arn:aws:iam::444455556666:role/ccf/destinations/CcfDestinationRole-docs",
        "arn:aws-cn:iam::111122223333:role/ccf/destinations/CcfDestinationRole-docs",
    ],
)
def test_create_rejects_a_role_outside_the_caller_account_or_partition(world: World, role_arn: str) -> None:
    with pytest.raises(BadRequestError) as raised:
        world.create(request(role_arn=role_arn))

    assert raised.value.fields == ("role_arn",)
    assert world.sfn.calls == []


@pytest.mark.parametrize(
    "role_arn",
    [
        "arn:aws:iam::111122223333:role/ccf/CcfDestinationRole",
        "arn:aws:iam::111122223333:role/ccf/destinations/CcfKnowledgeBase-dst-7c41e9a2b5d0",
        "arn:aws:iam::111122223333:role/ccf/destinations/CcfDestinationRole-",
        "arn:aws:iam::111122223333:role/ccf/destinations/CcfDestinationRole-docs/extra",
        "arn:aws:iam::111122223333:role/ccf/destinations/CcfDestinationRole-" + "a" * 46,
    ],
)
def test_the_request_model_rejects_a_role_outside_the_destination_role_path_or_name(role_arn: str) -> None:
    with pytest.raises(ValidationError) as raised:
        request(role_arn=role_arn)

    assert raised.value.errors()[0]["loc"] == ("role_arn",)


def test_create_whose_workflow_fails_to_start_leaves_no_item(world: World) -> None:
    world.sfn.fail_next_start = True

    with pytest.raises(DependencyUnavailableError):
        world.create(request(client_token="client-token-1"))

    assert world.resource.Table(world.names.destinations).scan()["Items"] == []


def test_create_names_each_destination_at_random(world: World) -> None:
    first = world.create(request()).destination
    second = world.create(request()).destination

    assert first.destination_id != second.destination_id
    assert len(world.sfn.started) == 2


def test_create_whose_compensation_fails_raises_the_start_failure_and_logs_the_compensation(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    world.fail(FailingCall(operation="DeleteItem", table_name=world.names.destinations))
    world.sfn.fail_next_start = True

    with pytest.raises(DependencyUnavailableError):
        world.create(request())

    [record] = [record for record in caplog.records if record.getMessage() == "destination_compensation_failed"]
    assert record.levelname == "ERROR"


def test_a_create_replayed_with_its_token_returns_the_same_destination_even_with_every_update_failing(
    world: World,
) -> None:
    world.fail(FailingCall(operation="UpdateItem", table_name=world.names.destinations))
    creates = idempotent_creates(world)

    def run() -> Any:
        call = world.request(request(client_token="client-token-1"))
        spec = CATALOG.spec(Operation.CREATE_DESTINATION)
        return creates.run(IdempotentRunRequest(spec=spec, request=call, activity=world.create_activity.create))

    first, second = run(), run()

    assert second == first
    assert len(world.sfn.started) == 1


@pytest.mark.parametrize("operation", ["get", "list", "delete"])
def test_another_tenants_destination_is_invisible_to_get_list_and_delete(world: World, operation: str) -> None:
    destination_id = world.create(request()).destination.destination_id
    calls: dict[str, Callable[[], Any]] = {
        "get": lambda: world.get(destination_id, OTHER_TENANT),
        "list": lambda: world.listing.list(world.request(ListDestinationsRequest(), OTHER_TENANT)),
        "delete": lambda: world.delete(destination_id, OTHER_TENANT),
    }

    if operation == "list":
        assert calls[operation]().destinations == []
        return
    with pytest.raises(ResourceNotFoundError):
        calls[operation]()


def test_list_returns_the_tenants_destinations_by_name(world: World) -> None:
    world.create(request(name="docs"))
    world.create(request(name="wiki"))

    listed = world.listing.list(world.request(ListDestinationsRequest(name="wiki")))

    assert [summary.name for summary in listed.destinations] == ["wiki"]  # type: ignore[attr-defined]


def test_delete_with_a_custom_connector_conflicts_and_restores_the_previous_status(world: World) -> None:
    destination_id = world.active_destination()
    world.add_connector(destination_id)

    with pytest.raises(ResourceConflictError):
        world.delete(destination_id)

    assert world.item(destination_id)["status"] == "ACTIVE"  # type: ignore[index]
    assert world.deletion_executions() == []


def test_delete_sets_deleting_before_it_checks_for_custom_connectors(world: World) -> None:
    destination_id = world.active_destination()
    seen: list[str] = []
    check = world.connectors.has_connectors

    def recording_check(destination: DestinationRef) -> bool:
        seen.append(world.item(destination_id)["status"])  # type: ignore[index]
        return check(destination)

    world.connectors.has_connectors = recording_check  # type: ignore[method-assign]

    world.delete(destination_id)

    assert seen == ["DELETING"]
    [started] = world.deletion_executions()
    assert started["name"] == f"{destination_id}-delete-c8f9c4b5"
    workflow_input = json.loads(started["input"])
    assert (workflow_input["action"], workflow_input["execution_name"]) == ("DELETE", started["name"])
    assert "boundary_arn" not in workflow_input


def test_delete_whose_workflow_fails_to_start_from_active_restores_the_previous_status(world: World) -> None:
    destination_id = world.active_destination()
    world.set_status(destination_id, DestinationStatus.FAILED)
    world.sfn.fail_next_start = True

    with pytest.raises(DependencyUnavailableError):
        world.delete(destination_id)

    item = world.item(destination_id)
    assert item["status"] == "FAILED"  # type: ignore[index]
    assert "previous_status" not in item  # type: ignore[operator]


def test_delete_whose_workflow_fails_to_start_on_a_deleting_resume_keeps_deleting(world: World) -> None:
    destination_id = world.active_destination()
    world.destinations.begin_deletion(DestinationRef(tenant=TENANT, destination_id=destination_id))
    world.sfn.fail_next_start = True

    with pytest.raises(DependencyUnavailableError):
        world.delete(destination_id)

    assert world.item(destination_id)["status"] == "DELETING"  # type: ignore[index]


@pytest.mark.parametrize(
    "status",
    [
        DestinationStatus.CREATING_BUCKET,
        DestinationStatus.CREATING_KNOWLEDGE_BASE,
        DestinationStatus.CREATING_DATA_SOURCE,
    ],
)
def test_delete_while_creating_conflicts_and_changes_nothing(world: World, status: DestinationStatus) -> None:
    destination_id = world.create(request()).destination.destination_id
    world.set_status(destination_id, status)
    before = world.item(destination_id)

    with pytest.raises(ResourceConflictError):
        world.delete(destination_id)

    assert world.item(destination_id) == before
    assert world.deletion_executions() == []


def test_repeated_delete_starts_nothing_while_the_recorded_execution_runs(world: World) -> None:
    destination_id = world.active_destination()
    world.delete(destination_id)
    world.sfn.statuses[world.record_last_execution(destination_id)] = "RUNNING"

    world.delete(destination_id)

    assert len(world.deletion_executions()) == 1


def test_repeated_delete_starts_a_new_execution_when_the_recorded_one_failed(world: World) -> None:
    destination_id = world.active_destination()
    world.delete(destination_id)
    world.sfn.statuses[world.record_last_execution(destination_id)] = "FAILED"
    world.trace = RequestTrace(correlation_id=CORRELATION_ID, api_request_id="d0a1b2c3-0000-4000-8000-000000000000")

    world.delete(destination_id)

    assert len(world.deletion_executions()) == 2


def test_repeated_delete_starts_an_execution_when_none_is_recorded(world: World) -> None:
    destination_id = world.active_destination()
    world.set_status(destination_id, DestinationStatus.DELETING)

    world.delete(destination_id)

    assert len(world.deletion_executions()) == 1


def test_repeated_delete_of_a_deleting_destination_with_a_custom_connector_conflicts_and_restores(
    world: World,
) -> None:
    destination_id = world.active_destination()
    world.add_connector(destination_id)
    world.destinations.begin_deletion(DestinationRef(tenant=TENANT, destination_id=destination_id))

    with pytest.raises(ResourceConflictError):
        world.delete(destination_id)

    assert world.item(destination_id)["status"] == "ACTIVE"  # type: ignore[index]
    assert world.deletion_executions() == []


def test_a_failed_destination_whose_reason_is_at_the_api_limit_can_be_read_and_deleted(world: World) -> None:
    destination_id = world.create(request()).destination.destination_id
    correlation = "(correlation 2f6c0e44-5a7b-4a8e-9a63-1d8f0c2b7e91)"
    head = "S3.InvalidRequestException: "
    reason = f"{head}{'x' * (2048 - len(head) - len(correlation) - 1)} {correlation}"
    world.resource.Table(world.names.destinations).update_item(
        Key={"tenant_arn_prefix": TENANT.arn_prefix, "destination_id": destination_id},
        UpdateExpression="SET #status = :failed, status_reason = :reason",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":failed": DestinationStatus.FAILED.value, ":reason": reason},
    )

    fetched = world.get(destination_id)
    world.delete(destination_id)

    assert fetched.destination.status_reason == reason
    assert world.item(destination_id)["status"] == "DELETING"  # type: ignore[index]


OWNER = MetadataField(name="owner", type=MetadataFieldType.STRING, searchability=MetadataFieldSearchability.ENABLED)
TOPICS = MetadataField(
    name="topics", type=MetadataFieldType.STRING_LIST, searchability=MetadataFieldSearchability.DISABLED
)


def update_request(destination_id: str, *fields: MetadataField) -> UpdateDestinationRequest:
    return UpdateDestinationRequest(destination_id=destination_id, metadata_fields=list(fields))


def bedrock_destination(world: World) -> str:
    destination_id = world.active_destination()
    world.set_knowledge_base(destination_id)
    return destination_id


def test_a_new_destination_has_no_metadata_fields(world: World) -> None:
    created = world.create(request()).destination

    assert world.get(created.destination_id).destination.metadata_fields == []


def test_update_sets_the_fields_on_the_knowledge_base_then_stores_and_returns_them(world: World) -> None:
    destination_id = bedrock_destination(world)

    updated = world.update(update_request(destination_id, OWNER, TOPICS)).destination

    assert world.knowledge_bases.calls == [
        SetMetadataFieldsRequest(
            destination_id=destination_id,
            role_arn=ROLE_ARN,
            knowledge_base_id=KNOWLEDGE_BASE_ID,
            fields=(OWNER, TOPICS),
        )
    ]
    assert (updated.metadata_fields, updated.status) == ([OWNER, TOPICS], DestinationStatus.ACTIVE)
    assert world.get(destination_id).destination.metadata_fields == [OWNER, TOPICS]
    assert world.sfn.calls == ["StartExecution"]


def test_update_with_an_empty_list_removes_every_field(world: World) -> None:
    destination_id = bedrock_destination(world)
    world.update(update_request(destination_id, OWNER))

    world.update(update_request(destination_id))

    assert world.knowledge_bases.calls[-1].fields == ()
    assert world.get(destination_id).destination.metadata_fields == []


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (MetadataFieldsRejectedError("rejected"), MetadataFieldsRejectedError),
        (KnowledgeBaseUpdatingError("updating"), ResourceConflictError),
    ],
)
def test_update_the_knowledge_base_refuses_raises_its_error_and_keeps_the_stored_fields(
    world: World, error: Exception, expected: type[Exception]
) -> None:
    destination_id = bedrock_destination(world)
    world.update(update_request(destination_id, OWNER))
    world.knowledge_bases.error = error

    with pytest.raises(expected):
        world.update(update_request(destination_id, TOPICS))

    assert world.get(destination_id).destination.metadata_fields == [OWNER]


@pytest.mark.parametrize(
    "status",
    [DestinationStatus.CREATING_KNOWLEDGE_BASE, DestinationStatus.FAILED, DestinationStatus.DELETING],
)
def test_update_of_a_destination_that_isnt_active_conflicts_and_changes_nothing(
    world: World, status: DestinationStatus
) -> None:
    destination_id = bedrock_destination(world)
    world.set_status(destination_id, status)

    with pytest.raises(ResourceConflictError):
        world.update(update_request(destination_id, OWNER))

    assert world.knowledge_bases.calls == []
    assert world.get(destination_id).destination.metadata_fields == []


@pytest.mark.parametrize("destination_type", [DestinationType.S3, DestinationType.QUICK])
def test_update_of_a_destination_without_a_knowledge_base_is_rejected(
    world: World, destination_type: DestinationType
) -> None:
    destination_id = world.create(request(type=destination_type)).destination.destination_id
    world.set_status(destination_id, DestinationStatus.ACTIVE)

    with pytest.raises(BadRequestError) as raised:
        world.update(update_request(destination_id, OWNER))

    assert raised.value.fields == ("metadata_fields",)
    assert world.knowledge_bases.calls == []


def test_another_tenants_destination_is_invisible_to_update(world: World) -> None:
    destination_id = bedrock_destination(world)

    with pytest.raises(ResourceNotFoundError):
        world.update(update_request(destination_id, OWNER), OTHER_TENANT)

    assert world.knowledge_bases.calls == []
