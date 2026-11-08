"""Job operations: start, get, list, and stop."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest
from botocore.exceptions import ClientError
from ccf_api_models.base import CcfModel
from ccf_api_models.catalog import OperationCatalog
from ccf_api_models.identifiers import ConnectorRef, TenantContext
from ccf_api_models.metric_names import MetricDimensionName, MetricName
from ccf_api_models.models import (
    ConnectorStatus,
    GetCustomConnectorJobRequest,
    JobStatus,
    ListCustomConnectorJobsRequest,
    StartCustomConnectorJobRequest,
    StopCustomConnectorJobRequest,
)
from ccf_api_models.operations import Operation
from pydantic import SecretStr

from activities.jobs import (
    GetCustomConnectorJobActivity,
    StartCustomConnectorJobActivity,
    StopCustomConnectorJobActivity,
)
from common.errors import BadRequestError, DependencyUnavailableError, ResourceConflictError, ResourceNotFoundError
from common.observability import MetricRecord, ServiceMetrics
from common.operation import OperationRequest, RequestTrace
from common.read_resources import ListResources
from common.resource_data_codec import ResourceDataCodec
from common.resource_ids import ResourceIds
from common.settings import TableNames
from common.workflow_executions import ExecutionStatus, WorkflowExecutions
from tests.unit.dao_world import JOB_SETTINGS, DaoTables, DaoWorld

TRACE = RequestTrace(correlation_id="correlation-1", api_request_id="request-1")
OTHER_TENANT = TenantContext.from_arn_prefix("arn:aws:ccf:us-east-1:999988887777")


class FakeStepFunctions:
    """Records `StartExecution` and `StopExecution`; `on_stop` runs as the execution stops."""

    def __init__(self) -> None:
        self.started: list[dict[str, Any]] = []
        self.stopped: list[str] = []
        self.fail_start = False
        self.on_stop: Any = None
        self.execution_status = ExecutionStatus.RUNNING
        """What `DescribeExecution` answers for every execution."""

    def describe_execution(self, **arguments: Any) -> dict[str, Any]:
        return {"executionArn": arguments["executionArn"], "status": self.execution_status.value}

    def start_execution(self, **arguments: Any) -> dict[str, Any]:
        if self.fail_start:
            raise ClientError({"Error": {"Code": "ServiceUnavailable", "Message": "down"}}, "StartExecution")
        self.started.append(arguments)
        return {"executionArn": f"arn:aws:states:us-east-1:444455556666:execution:CcfJobExecution:{arguments['name']}"}

    def stop_execution(self, **arguments: Any) -> dict[str, Any]:
        self.stopped.append(arguments["executionArn"])
        if self.on_stop is not None:
            self.on_stop()
        return {}


class RecordingMetrics(ServiceMetrics):
    def __init__(self) -> None:
        self.records: list[MetricRecord] = []

    def record(self, metric: MetricRecord) -> None:
        self.records.append(metric)


class JobWorld:
    """The DAO world, the three job activities over it, and the listing as the API composes it."""

    def __init__(self, daos: DaoWorld, codec: ResourceDataCodec) -> None:
        self.daos = daos
        self.sfn = FakeStepFunctions()
        self.metrics = RecordingMetrics()
        executions = WorkflowExecutions(self.sfn, JOB_SETTINGS.job_execution_arn)  # type: ignore[arg-type]
        self.start_activity = StartCustomConnectorJobActivity(daos.jobs, codec, executions, ResourceIds())
        self.get_activity = GetCustomConnectorJobActivity(daos.jobs, codec)
        self.stop_activity = StopCustomConnectorJobActivity(daos.jobs, executions, self.metrics)
        self.list_jobs = ListResources(
            daos.jobs.list,
            ConnectorRef.of,
            OperationCatalog.load().spec(Operation.LIST_CUSTOM_CONNECTOR_JOBS),
            daos.connectors.exists,
        )

    def request[PayloadT: CcfModel](self, payload: PayloadT) -> OperationRequest[PayloadT]:
        return OperationRequest(tenant=self.daos.tenant, payload=payload, trace=TRACE)

    def start(self, configuration: str | None = None) -> str:
        connector = self.daos.connector
        payload = StartCustomConnectorJobRequest(
            destination_id=connector.destination_id,
            connector_id=connector.connector_id,
            job_configuration=None if configuration is None else SecretStr(configuration),
        )
        return self.start_activity.start(self.request(payload)).job.job_id

    def get(self, job_id: str) -> Any:
        connector = self.daos.connector
        payload = GetCustomConnectorJobRequest(
            destination_id=connector.destination_id, connector_id=connector.connector_id, job_id=job_id
        )
        return self.get_activity.fetch(self.request(payload)).job

    def stop(self, job_id: str) -> None:
        connector = self.daos.connector
        payload = StopCustomConnectorJobRequest(
            destination_id=connector.destination_id, connector_id=connector.connector_id, job_id=job_id
        )
        self.stop_activity.stop(self.request(payload))

    def job_item(self, job_id: str) -> dict[str, Any] | None:
        key = {"tenant_arn_prefix": self.daos.tenant.arn_prefix, "job_id": job_id}
        item: dict[str, Any] | None = self.daos.table(self.daos.names.jobs).get_item(Key=key).get("Item")
        return item

    def set_connector_status(self, status: ConnectorStatus) -> None:
        self.daos.table(self.daos.names.connectors).update_item(
            Key={
                "tenant_arn_prefix": self.daos.tenant.arn_prefix,
                "destination_connector_key": self.daos.connector.destination_connector_key,
            },
            UpdateExpression="SET #status = :status",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":status": status.value},
        )

    def connector_status(self) -> ConnectorStatus:
        return self.daos.connectors.get(self.daos.connector).status


@pytest.fixture
def world(dynamodb: Any, table_names: TableNames, codec: ResourceDataCodec) -> JobWorld:
    built = JobWorld(DaoWorld(DaoTables(resource=dynamodb, names=table_names, codec=codec)), codec)
    built.daos.available_connector()
    return built


@pytest.mark.parametrize("connector_status", [ConnectorStatus.IN_USE, ConnectorStatus.DELETING])
def test_start_while_the_custom_connector_is_in_use_or_deleting_conflicts(
    world: JobWorld, connector_status: ConnectorStatus
) -> None:
    world.set_connector_status(connector_status)

    with pytest.raises(ResourceConflictError):
        world.start()


def test_start_while_another_job_runs_conflicts_and_leaves_that_job_alone(world: JobWorld) -> None:
    running = world.start()

    with pytest.raises(ResourceConflictError):
        world.start()

    item = world.job_item(running)
    assert item is not None and item["status"] == JobStatus.STARTED.value


@pytest.mark.parametrize("ended", [ExecutionStatus.FAILED, ExecutionStatus.TIMED_OUT, ExecutionStatus.ABORTED])
def test_start_stops_a_holding_job_whose_execution_ended_and_starts(world: JobWorld, ended: ExecutionStatus) -> None:
    abandoned = world.start()
    world.sfn.execution_status = ended

    started = world.start()

    item = world.job_item(abandoned)
    assert item is not None and item["status"] == JobStatus.STOPPED.value
    assert [execution["name"] for execution in world.sfn.started] == [abandoned, started]


def test_start_while_the_destination_is_not_active_conflicts(world: JobWorld) -> None:
    world.daos.set_destination_status(world.daos.StatusChange(status="DELETING"))

    with pytest.raises(ResourceConflictError):
        world.start()


def test_start_for_a_missing_custom_connector_is_not_found(world: JobWorld) -> None:
    payload = StartCustomConnectorJobRequest(destination_id="dst-0a1b2c3d4e5f", connector_id="cc-e020d65d76c0")

    with pytest.raises(ResourceNotFoundError):
        world.start_activity.start(world.request(payload))


def test_start_starts_one_execution_named_by_the_job_id(world: JobWorld) -> None:
    job_id = world.start()

    [started] = world.sfn.started
    assert started["name"] == job_id
    assert json.loads(started["input"]) == {
        "tenant_arn_prefix": world.daos.tenant.arn_prefix,
        "destination_id": world.daos.connector.destination_id,
        "connector_id": world.daos.connector.connector_id,
        "job_id": job_id,
        "correlation_id": "correlation-1",
    }


def test_a_failed_execution_start_undoes_the_start_and_raises_its_error(world: JobWorld) -> None:
    world.sfn.fail_start = True

    with pytest.raises(DependencyUnavailableError):
        world.start()

    assert world.daos.table(world.daos.names.jobs).scan()["Items"] == []
    assert world.connector_status() is ConnectorStatus.AVAILABLE


@pytest.mark.parametrize("configuration", ['{"token": "x"}', "full-read please"])
def test_job_configuration_is_stored_and_returned_unchanged(world: JobWorld, configuration: str) -> None:
    job = world.get(world.start(configuration))

    assert job.status is JobStatus.STARTED
    assert job.job_configuration.get_secret_value() == configuration


def test_a_job_without_a_configuration_returns_none(world: JobWorld) -> None:
    assert world.get(world.start()).job_configuration is None


def test_job_configuration_one_byte_over_the_limit_is_rejected(world: JobWorld) -> None:
    too_large = "€" * 21_845 + "aa"

    with pytest.raises(BadRequestError) as raised:
        world.start(too_large)

    assert raised.value.fields == ("job_configuration",)


def test_list_returns_the_custom_connectors_jobs_newest_first(world: JobWorld) -> None:
    first = world.start()
    world.stop(first)
    second = world.start()
    connector = world.daos.connector
    payload = ListCustomConnectorJobsRequest(
        destination_id=connector.destination_id, connector_id=connector.connector_id
    )

    listed = world.list_jobs.list(world.request(payload))

    assert [job.job_id for job in listed.jobs] == [second, first]  # type: ignore[attr-defined]


def test_list_for_a_missing_custom_connector_is_not_found(world: JobWorld) -> None:
    payload = ListCustomConnectorJobsRequest(destination_id="dst-0a1b2c3d4e5f", connector_id="cc-e020d65d76c0")

    with pytest.raises(ResourceNotFoundError):
        world.list_jobs.list(world.request(payload))


def test_stop_ends_the_job_stopped_releases_the_connector_and_stops_its_execution(world: JobWorld) -> None:
    job_id = world.start()

    world.stop(job_id)

    item = world.job_item(job_id)
    assert item is not None and item["status"] == JobStatus.STOPPED.value
    assert world.connector_status() is ConnectorStatus.AVAILABLE
    assert world.sfn.stopped == [f"{JOB_SETTINGS.job_execution_arn.replace(':stateMachine:', ':execution:')}:{job_id}"]


def test_stop_publishes_jobs_completed_and_job_duration_when_it_ends_the_job(world: JobWorld) -> None:
    world.stop(world.start())

    connector_id = world.daos.connector.connector_id
    published = [(record.name, dict(record.dimensions)) for record in world.metrics.records]
    assert published == [
        (MetricName.JOBS_COMPLETED, {MetricDimensionName.STATUS: "STOPPED"}),
        (
            MetricName.JOBS_COMPLETED,
            {MetricDimensionName.STATUS: "STOPPED", MetricDimensionName.CONNECTOR_ID: connector_id},
        ),
        (MetricName.JOB_DURATION, {MetricDimensionName.CONNECTOR_ID: connector_id}),
    ]


def test_stop_publishes_nothing_when_the_execution_ended_the_job_first(world: JobWorld) -> None:
    job_id = world.start()
    world.sfn.on_stop = lambda: world.daos.table(world.daos.names.jobs).update_item(
        Key={"tenant_arn_prefix": world.daos.tenant.arn_prefix, "job_id": job_id},
        UpdateExpression="SET #status = :stopped",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":stopped": JobStatus.STOPPED.value},
    )

    world.stop(job_id)

    assert world.metrics.records == []


@pytest.mark.parametrize("terminal", [JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.STOPPED])
def test_stop_of_a_job_that_ended_conflicts_and_stops_no_execution(world: JobWorld, terminal: JobStatus) -> None:
    job_id = world.start()
    world.daos.table(world.daos.names.jobs).update_item(
        Key={"tenant_arn_prefix": world.daos.tenant.arn_prefix, "job_id": job_id},
        UpdateExpression="SET #status = :status",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":status": terminal.value},
    )

    with pytest.raises(ResourceConflictError):
        world.stop(job_id)

    assert world.sfn.stopped == []


@pytest.mark.parametrize("operation", ["start", "get", "list", "stop"])
def test_every_job_operation_from_another_tenant_is_not_found_and_writes_nothing(
    world: JobWorld, operation: str
) -> None:
    job_id = world.start()
    jobs_table = world.daos.table(world.daos.names.jobs)
    before = jobs_table.scan()["Items"]
    destination_id, connector_id = world.daos.connector.destination_id, world.daos.connector.connector_id

    def outside[PayloadT: CcfModel](payload: PayloadT) -> OperationRequest[PayloadT]:
        return OperationRequest(tenant=OTHER_TENANT, payload=payload, trace=TRACE)

    calls: dict[str, Callable[[], object]] = {
        "start": lambda: world.start_activity.start(
            outside(StartCustomConnectorJobRequest(destination_id=destination_id, connector_id=connector_id))
        ),
        "get": lambda: world.get_activity.fetch(
            outside(
                GetCustomConnectorJobRequest(destination_id=destination_id, connector_id=connector_id, job_id=job_id)
            )
        ),
        "list": lambda: world.list_jobs.list(
            outside(ListCustomConnectorJobsRequest(destination_id=destination_id, connector_id=connector_id))
        ),
        "stop": lambda: world.stop_activity.stop(
            outside(
                StopCustomConnectorJobRequest(destination_id=destination_id, connector_id=connector_id, job_id=job_id)
            )
        ),
    }

    with pytest.raises(ResourceNotFoundError):
        calls[operation]()

    assert jobs_table.scan()["Items"] == before
