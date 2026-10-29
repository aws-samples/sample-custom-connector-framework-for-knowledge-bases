"""Connector and job activity tests for encrypted immutable configuration."""

import hashlib
from typing import Any
from unittest.mock import MagicMock, patch

import boto3
import pytest
from ccf_api_models.models import (
    ConnectorSchedule,
    ConnectorStatus,
    ContainerProperties,
    CreateCustomConnectorRequest,
    DeleteCustomConnectorRequest,
    DestinationMode,
    GetCustomConnectorJobRequest,
    GetCustomConnectorRequest,
    JobStatus,
    JobSummary,
    ResourceRequirements,
    ScheduleState,
    ServiceExecution,
    SourceAccessConfiguration,
    StartCustomConnectorJobRequest,
    UpdateCustomConnectorRequest,
)
from moto import mock_aws
from pydantic import SecretStr

from activities.connectors import (
    ConnectorOwnershipValidator,
    CreateCustomConnectorActivity,
    DeleteCustomConnectorActivity,
    GetCustomConnectorActivity,
    ServiceReferenceConfiguration,
    UpdateCustomConnectorActivity,
)
from activities.jobs import GetCustomConnectorJobActivity, StartCustomConnectorJobActivity
from common.configuration import (
    ConfigurationValidationError,
    ConfigurationValidator,
    DecryptConfigurationRequest,
    EncryptConfigurationRequest,
)
from common.operation import OperationRequest as _OperationRequest
from common.runtime_role import RuntimeRoleError, RuntimeRoleIdentity, RuntimeRolePolicyRequest
from common.storage.ddb.custom_connector_jobs_dao import (
    CustomConnectorJobsDao,
    GetJobRequest,
    StartJobRequest,
    UpdateJobStatusRequest,
)
from common.storage.ddb.custom_connectors_dao import (
    ClaimConnectorDeletionRequest,
    CustomConnectorsDao,
    GetConnectorRequest,
    UpdateConnectorStatusRequest,
)
from common.storage.errors import StorageConflictError, StorageNotFoundError, StorageOperationError
from common.tenant import TenantContext


class FakeConfigurationCodec:
    """Opaque deterministic test codec with explicit context binding."""

    def __init__(self) -> None:
        self._plaintext_by_ciphertext: dict[bytes, tuple[str, str]] = {}

    def encrypt(self, request: EncryptConfigurationRequest) -> bytes:
        plaintext = request.configuration.get_secret_value()
        ciphertext = hashlib.sha256(f"{request.resource_arn}:{plaintext}".encode()).digest()
        self._plaintext_by_ciphertext[ciphertext] = (request.resource_arn, plaintext)
        return ciphertext

    def decrypt(self, request: DecryptConfigurationRequest) -> SecretStr:
        resource_arn, plaintext = self._plaintext_by_ciphertext[request.ciphertext]
        if resource_arn != request.resource_arn:
            raise ValueError("wrong context")
        return SecretStr(plaintext)


def create_tables():
    client = boto3.client("dynamodb", region_name="us-east-1")
    client.create_table(
        TableName="connectors",
        KeySchema=[
            {"AttributeName": "custom_connector_arn_prefix", "KeyType": "HASH"},
            {"AttributeName": "connector_id", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "custom_connector_arn_prefix", "AttributeType": "S"},
            {"AttributeName": "connector_id", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    client.create_table(
        TableName="jobs",
        KeySchema=[
            {"AttributeName": "custom_connector_arn_prefix", "KeyType": "HASH"},
            {"AttributeName": "job_id", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "custom_connector_arn_prefix", "AttributeType": "S"},
            {"AttributeName": "job_id", "AttributeType": "S"},
            {"AttributeName": "connector_id", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "GSI1",
                "KeySchema": [
                    {"AttributeName": "custom_connector_arn_prefix", "KeyType": "HASH"},
                    {"AttributeName": "connector_id", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    resource = boto3.resource("dynamodb", region_name="us-east-1")
    connectors = CustomConnectorsDao(resource.Table("connectors"))
    return connectors, CustomConnectorJobsDao(resource.Table("jobs"), connectors, client)


def create_payload(configuration: str = '{"repository":"one"}') -> CreateCustomConnectorRequest:
    return CreateCustomConnectorRequest(
        name="connector",
        container_properties=ContainerProperties(
            image_uri="123456789012.dkr.ecr.us-east-1.amazonaws.com/connector:latest",
            command=["ccf-connector", "run", "connector:definition"],
            resource_requirements=ResourceRequirements(cpu=1.0, memory=2048),
            timeout=3600,
        ),
        connector_configuration=SecretStr(configuration),
        source_access=SourceAccessConfiguration(
            role_arn="arn:aws:iam::123456789012:role/CcfSourceRole-connector",
            external_id="external-id",
        ),
    )


_RUNTIME_ROLE_ARN = "arn:aws:iam::210987654321:role/ccf-runtime/CcfConnectorRuntime-0123456789abcdef0123456789abcdef"
_REST_API_ID = "abc123def4"
_CLIENT_ACCOUNT_IDS = frozenset({"123456789012"})


def operation_request(
    tenant: TenantContext,
    payload: Any,
    idempotency_key: str | None = None,
    service_endpoint: str | None = None,
    rest_api_id: str | None = _REST_API_ID,
) -> _OperationRequest[Any]:
    """Build one operation request whose serving REST API id defaults to a fixed id."""
    return _OperationRequest(tenant, payload, idempotency_key, service_endpoint, rest_api_id)


def runtime_roles_double() -> MagicMock:
    """A runtime-role lifecycle double that vends one deterministic role ARN."""
    double = MagicMock()
    double.create.return_value = _RUNTIME_ROLE_ARN
    return double


def create_connector_activity(
    connectors: CustomConnectorsDao,
    validator: ConfigurationValidator,
    codec: FakeConfigurationCodec,
    schedule_lifecycle: MagicMock,
    runtime_roles: MagicMock | None = None,
) -> CreateCustomConnectorActivity:
    return CreateCustomConnectorActivity(
        connectors,
        validator,
        codec,
        schedule_lifecycle,
        ConnectorOwnershipValidator(_CLIENT_ACCOUNT_IDS),
        runtime_roles or runtime_roles_double(),
    )


def update_connector_activity(
    connectors: CustomConnectorsDao,
    validator: ConfigurationValidator,
    codec: FakeConfigurationCodec,
    schedule_lifecycle: MagicMock,
    runtime_roles: MagicMock | None = None,
) -> UpdateCustomConnectorActivity:
    return UpdateCustomConnectorActivity(
        connectors,
        validator,
        codec,
        schedule_lifecycle,
        ConnectorOwnershipValidator(_CLIENT_ACCOUNT_IDS),
        runtime_roles or runtime_roles_double(),
    )


def delete_connector_activity(
    connectors: CustomConnectorsDao,
    schedule_lifecycle: MagicMock,
    runtime_roles: MagicMock | None = None,
) -> DeleteCustomConnectorActivity:
    return DeleteCustomConnectorActivity(
        connectors,
        schedule_lifecycle,
        runtime_roles or runtime_roles_double(),
    )


@mock_aws
def test_connector_configuration_is_ciphertext_and_get_reveals_authorized_plaintext() -> None:
    connectors, _ = create_tables()
    codec = FakeConfigurationCodec()
    tenant = TenantContext("123456789012", "us-east-1")
    created = create_connector_activity(
        connectors,
        ConfigurationValidator(),
        codec,
        MagicMock(),
    )
    response = created.create(operation_request(tenant, create_payload()))
    connector_id = response.connector.connector_id

    raw = connectors.table.get_item(
        Key={"custom_connector_arn_prefix": tenant.get_arn_prefix(), "connector_id": connector_id}
    )["Item"]
    assert "connector_configuration" not in raw
    assert bytes(raw["encrypted_configuration"]) != b'{"repository":"one"}'

    fetched = GetCustomConnectorActivity(
        connectors,
        codec,
        ServiceReferenceConfiguration(
            account_id="999999999999",
            endpoint="https://ccf.example.com/prod",
            region="us-west-2",
            partition="aws-us-gov",
            service_principal="ccf.amazonaws.com",
            ingestion_bucket_name="ingestion-bucket",
            destination_mode=DestinationMode.S3,
        ),
    ).fetch(
        operation_request(
            tenant,
            GetCustomConnectorRequest(connector_id=connector_id),
            service_endpoint="https://request-context.example.com/prod",
        )
    )
    assert fetched.connector.connector_configuration.get_secret_value() == '{"repository":"one"}'
    assert fetched.service_reference.endpoint == "https://ccf.example.com/prod"
    assert fetched.service_reference.region == "us-west-2"
    assert fetched.service_reference.service_account_id == "999999999999"
    assert fetched.service_reference.service_principal == "ccf.amazonaws.com"
    assert fetched.service_reference.destination.bucket_arn == ("arn:aws-us-gov:s3:::ingestion-bucket")


@mock_aws
def test_connector_configuration_update_rejects_while_in_use() -> None:
    connectors, _ = create_tables()
    codec = FakeConfigurationCodec()
    validator = ConfigurationValidator()
    tenant = TenantContext("123456789012", "us-east-1")
    created = create_connector_activity(connectors, validator, codec, MagicMock()).create(
        operation_request(tenant, create_payload())
    )
    connector_id = created.connector.connector_id
    connectors.update_connector_status(
        UpdateConnectorStatusRequest(tenant, connector_id, ConnectorStatus.IN_USE, "job-1")
    )

    with pytest.raises(StorageConflictError):
        update_connector_activity(connectors, validator, codec, MagicMock()).update(
            operation_request(
                tenant,
                UpdateCustomConnectorRequest(
                    connector_id=connector_id,
                    connector_configuration=SecretStr('{"repository":"two"}'),
                ),
            )
        )


@mock_aws
def test_connector_configuration_update_replaces_ciphertext_without_history() -> None:
    connectors, _ = create_tables()
    codec = FakeConfigurationCodec()
    validator = ConfigurationValidator()
    tenant = TenantContext("123456789012", "us-east-1")
    connector = (
        create_connector_activity(connectors, validator, codec, MagicMock())
        .create(operation_request(tenant, create_payload()))
        .connector
    )
    key = {
        "custom_connector_arn_prefix": tenant.get_arn_prefix(),
        "connector_id": connector.connector_id,
    }
    before = bytes(connectors.table.get_item(Key=key)["Item"]["encrypted_configuration"])

    update_connector_activity(connectors, validator, codec, MagicMock()).update(
        operation_request(
            tenant,
            UpdateCustomConnectorRequest(
                connector_id=connector.connector_id,
                connector_configuration=SecretStr('{"repository":"two"}'),
            ),
        )
    )
    item = connectors.table.get_item(Key=key)["Item"]

    assert bytes(item["encrypted_configuration"]) != before
    assert "configuration_history" not in item
    assert "connector_configuration" not in item


@mock_aws
def test_job_get_is_connector_scoped_and_status_update_keeps_configuration() -> None:
    connectors, jobs = create_tables()
    codec = FakeConfigurationCodec()
    validator = ConfigurationValidator()
    tenant = TenantContext("123456789012", "us-east-1")
    connector = (
        create_connector_activity(connectors, validator, codec, MagicMock())
        .create(operation_request(tenant, create_payload()))
        .connector
    )
    other_connector = (
        create_connector_activity(connectors, validator, codec, MagicMock())
        .create(operation_request(tenant, create_payload('{"repository":"other"}')))
        .connector
    )
    started = (
        StartCustomConnectorJobActivity(jobs, validator, codec)
        .start(
            operation_request(
                tenant,
                StartCustomConnectorJobRequest(
                    connector_id=connector.connector_id,
                    job_configuration=SecretStr('{"full_sync":true}'),
                ),
            )
        )
        .job
    )
    before = jobs.get_job(GetJobRequest(tenant, connector.connector_id, started.job_id))

    service_execution = ServiceExecution(
        execution_id="batch-1",
        execution_role_arn="arn:aws:iam::123456789012:role/framework-execution",
        job_role_arn="arn:aws:iam::123456789012:role/framework-job",
        started_at=2.0,
    )
    jobs.update_job_status(
        UpdateJobStatusRequest(
            tenant,
            connector.connector_id,
            started.job_id,
            started.status.RUNNING,
            batch_job_id="batch-1",
            service_execution=service_execution,
        )
    )
    after = jobs.get_job(GetJobRequest(tenant, connector.connector_id, started.job_id))
    assert after.encrypted_configuration == before.encrypted_configuration
    assert after.service_execution == service_execution

    fetched = GetCustomConnectorJobActivity(jobs, codec).fetch(
        operation_request(
            tenant,
            GetCustomConnectorJobRequest(
                connector_id=connector.connector_id,
                job_id=started.job_id,
            ),
        )
    )
    assert fetched.job.job_configuration.get_secret_value() == '{"full_sync":true}'

    with pytest.raises(StorageNotFoundError):
        jobs.get_job(GetJobRequest(tenant, other_connector.connector_id, started.job_id))
    with pytest.raises(StorageNotFoundError):
        jobs.get_job(
            GetJobRequest(
                TenantContext("210987654321", "us-east-1"),
                connector.connector_id,
                started.job_id,
            )
        )


@mock_aws
def test_job_transitions_are_monotonic_and_stale_terminal_delivery_preserves_new_reservation() -> None:
    connectors, jobs = create_tables()
    codec = FakeConfigurationCodec()
    tenant = TenantContext("123456789012", "us-east-1")
    connector = (
        create_connector_activity(connectors, ConfigurationValidator(), codec, MagicMock())
        .create(operation_request(tenant, create_payload()))
        .connector
    )
    first_job = jobs.start_job(StartJobRequest(tenant, connector.connector_id, b"first", job_id="job-first"))

    jobs.update_job_status(UpdateJobStatusRequest(tenant, connector.connector_id, first_job.job_id, JobStatus.RUNNING))
    jobs.update_job_status(
        UpdateJobStatusRequest(tenant, connector.connector_id, first_job.job_id, JobStatus.SUCCEEDED)
    )
    terminal_job = jobs.get_job(GetJobRequest(tenant, connector.connector_id, first_job.job_id))
    assert terminal_job.status is JobStatus.SUCCEEDED
    assert terminal_job.version == 3

    with pytest.raises(StorageConflictError, match="transition is invalid"):
        jobs.update_job_status(
            UpdateJobStatusRequest(tenant, connector.connector_id, first_job.job_id, JobStatus.RUNNING)
        )

    second_job = jobs.start_job(StartJobRequest(tenant, connector.connector_id, b"second", job_id="job-second"))
    jobs.update_job_status(
        UpdateJobStatusRequest(tenant, connector.connector_id, first_job.job_id, JobStatus.SUCCEEDED)
    )

    connector_item = connectors.table.get_item(
        Key={
            "custom_connector_arn_prefix": tenant.get_arn_prefix(),
            "connector_id": connector.connector_id,
        }
    )["Item"]
    assert connector_item["status"] == ConnectorStatus.IN_USE.value
    assert connector_item["active_job_id"] == second_job.job_id


def connector_schedule() -> ConnectorSchedule:
    return ConnectorSchedule(
        expression="rate(30 minutes)",
        state=ScheduleState.ENABLED,
        timezone="UTC",
    )


@mock_aws
def test_create_connector_reconciles_service_owned_schedule() -> None:
    connectors, _ = create_tables()
    codec = FakeConfigurationCodec()
    tenant = TenantContext("123456789012", "us-east-1")
    schedule_lifecycle = MagicMock()
    payload = create_payload().model_copy(update={"schedule": connector_schedule()})

    connector = (
        create_connector_activity(
            connectors,
            ConfigurationValidator(),
            codec,
            schedule_lifecycle,
        )
        .create(operation_request(tenant, payload))
        .connector
    )

    schedule_request = schedule_lifecycle.create.call_args.args[0]
    assert schedule_request.identity.connector_id == connector.connector_id
    assert schedule_request.identity.tenant_context == tenant
    assert schedule_request.schedule == payload.schedule


@mock_aws
def test_update_connector_reconciles_replacement_schedule() -> None:
    connectors, _ = create_tables()
    codec = FakeConfigurationCodec()
    validator = ConfigurationValidator()
    tenant = TenantContext("123456789012", "us-east-1")
    connector = (
        create_connector_activity(connectors, validator, codec, MagicMock())
        .create(operation_request(tenant, create_payload()))
        .connector
    )
    schedule_lifecycle = MagicMock()
    schedule = connector_schedule()

    update_connector_activity(connectors, validator, codec, schedule_lifecycle).update(
        operation_request(
            tenant,
            UpdateCustomConnectorRequest(connector_id=connector.connector_id, schedule=schedule),
        )
    )

    schedule_request = schedule_lifecycle.update.call_args.args[0]
    assert schedule_request.identity.connector_id == connector.connector_id
    assert schedule_request.schedule == schedule


@mock_aws
def test_delete_connector_removes_service_owned_schedule() -> None:
    connectors, _ = create_tables()
    codec = FakeConfigurationCodec()
    tenant = TenantContext("123456789012", "us-east-1")
    connector = (
        create_connector_activity(
            connectors,
            ConfigurationValidator(),
            codec,
            MagicMock(),
        )
        .create(operation_request(tenant, create_payload()))
        .connector
    )
    schedule_lifecycle = MagicMock()

    delete_connector_activity(connectors, schedule_lifecycle).delete(
        operation_request(
            tenant,
            DeleteCustomConnectorRequest(connector_id=connector.connector_id),
        )
    )

    identity = schedule_lifecycle.delete.call_args.args[0]
    assert identity.connector_id == connector.connector_id
    assert identity.tenant_context == tenant


@mock_aws
def test_create_schedule_failure_removes_schedule_and_connector_record() -> None:
    connectors, _ = create_tables()
    codec = FakeConfigurationCodec()
    tenant = TenantContext("123456789012", "us-east-1")
    schedule_lifecycle = MagicMock()
    schedule_lifecycle.create.side_effect = RuntimeError("schedule create failed")
    payload = create_payload().model_copy(update={"schedule": connector_schedule()})

    with pytest.raises(RuntimeError, match="schedule create failed"):
        create_connector_activity(
            connectors,
            ConfigurationValidator(),
            codec,
            schedule_lifecycle,
        ).create(operation_request(tenant, payload))

    identity = schedule_lifecycle.delete.call_args.args[0]
    with pytest.raises(StorageNotFoundError):
        connectors.get_connector(GetConnectorRequest(tenant, identity.connector_id))


@mock_aws
def test_update_schedule_failure_restores_deployed_and_stored_schedule() -> None:
    connectors, _ = create_tables()
    codec = FakeConfigurationCodec()
    validator = ConfigurationValidator()
    tenant = TenantContext("123456789012", "us-east-1")
    previous_schedule = connector_schedule()
    payload = create_payload().model_copy(update={"schedule": previous_schedule})
    connector = (
        create_connector_activity(connectors, validator, codec, MagicMock())
        .create(operation_request(tenant, payload))
        .connector
    )
    replacement_schedule = previous_schedule.model_copy(update={"expression": "rate(60 minutes)"})
    schedule_lifecycle = MagicMock()
    schedule_lifecycle.update.side_effect = [RuntimeError("schedule update failed"), None]

    with pytest.raises(RuntimeError, match="schedule update failed"):
        update_connector_activity(connectors, validator, codec, schedule_lifecycle).update(
            operation_request(
                tenant,
                UpdateCustomConnectorRequest(
                    connector_id=connector.connector_id,
                    schedule=replacement_schedule,
                ),
            )
        )

    assert schedule_lifecycle.update.call_count == 2
    restore_request = schedule_lifecycle.update.call_args_list[1].args[0]
    assert restore_request.schedule == previous_schedule
    stored = connectors.get_connector(GetConnectorRequest(tenant, connector.connector_id))
    assert stored.schedule == previous_schedule


@mock_aws
def test_delete_in_use_connector_does_not_remove_schedule() -> None:
    connectors, _ = create_tables()
    codec = FakeConfigurationCodec()
    tenant = TenantContext("123456789012", "us-east-1")
    payload = create_payload().model_copy(update={"schedule": connector_schedule()})
    connector = (
        create_connector_activity(
            connectors,
            ConfigurationValidator(),
            codec,
            MagicMock(),
        )
        .create(operation_request(tenant, payload))
        .connector
    )
    connectors.update_connector_status(
        UpdateConnectorStatusRequest(tenant, connector.connector_id, ConnectorStatus.IN_USE, "job-1")
    )
    schedule_lifecycle = MagicMock()

    with pytest.raises(StorageConflictError, match="Connector is in use"):
        delete_connector_activity(connectors, schedule_lifecycle).delete(
            operation_request(
                tenant,
                DeleteCustomConnectorRequest(connector_id=connector.connector_id),
            )
        )

    schedule_lifecycle.delete.assert_not_called()
    stored = connectors.get_connector(GetConnectorRequest(tenant, connector.connector_id))
    assert stored.status is ConnectorStatus.IN_USE


@mock_aws
def test_connector_delete_failure_restores_previous_schedule() -> None:
    connectors, _ = create_tables()
    codec = FakeConfigurationCodec()
    tenant = TenantContext("123456789012", "us-east-1")
    schedule = connector_schedule()
    payload = create_payload().model_copy(update={"schedule": schedule})
    connector = (
        create_connector_activity(
            connectors,
            ConfigurationValidator(),
            codec,
            MagicMock(),
        )
        .create(operation_request(tenant, payload))
        .connector
    )
    schedule_lifecycle = MagicMock()

    with (
        patch.object(
            connectors,
            "delete_connector",
            side_effect=StorageOperationError("connector delete failed"),
        ),
        pytest.raises(StorageOperationError, match="connector delete failed"),
    ):
        delete_connector_activity(connectors, schedule_lifecycle).delete(
            operation_request(
                tenant,
                DeleteCustomConnectorRequest(connector_id=connector.connector_id),
            )
        )

    schedule_lifecycle.delete.assert_called_once()
    restore_request = schedule_lifecycle.create.call_args.args[0]
    assert restore_request.schedule == schedule
    assert restore_request.identity.connector_id == connector.connector_id


def test_start_activity_uses_deterministic_id_only_for_scheduled_occurrences() -> None:
    tenant = TenantContext("123456789012", "us-east-1")
    dao = MagicMock()
    dao.start_job.return_value = JobSummary(
        job_id="stored-job",
        connector_id="connector-1",
        status=JobStatus.STARTED,
        created_at=1.0,
        updated_at=1.0,
    )
    activity = StartCustomConnectorJobActivity(
        dao,
        ConfigurationValidator(),
        FakeConfigurationCodec(),
    )
    payload = StartCustomConnectorJobRequest(
        connector_id="connector-1",
        job_configuration=SecretStr("{}"),
    )

    activity.start(operation_request(tenant, payload))
    first_manual_id = dao.start_job.call_args.args[0].job_id
    activity.start(operation_request(tenant, payload))
    second_manual_id = dao.start_job.call_args.args[0].job_id
    activity.start(
        operation_request(
            tenant,
            payload,
            idempotency_key="2026-10-10T03:04:05.000000+00:00",
        )
    )
    first_scheduled_request = dao.start_job.call_args.args[0]
    activity.start(
        operation_request(
            tenant,
            payload,
            idempotency_key="2026-10-10T03:04:05.000000+00:00",
        )
    )
    second_scheduled_request = dao.start_job.call_args.args[0]

    assert first_manual_id != second_manual_id
    assert first_scheduled_request.job_id == second_scheduled_request.job_id
    assert first_scheduled_request.job_id.startswith("ccj-")
    assert first_scheduled_request.idempotency_key is not None


@mock_aws
def test_scheduled_activity_returns_same_active_and_terminal_job() -> None:
    connectors, jobs = create_tables()
    codec = FakeConfigurationCodec()
    tenant = TenantContext("123456789012", "us-east-1")
    connector = (
        create_connector_activity(
            connectors,
            ConfigurationValidator(),
            codec,
            MagicMock(),
        )
        .create(operation_request(tenant, create_payload()))
        .connector
    )
    activity = StartCustomConnectorJobActivity(
        jobs,
        ConfigurationValidator(),
        codec,
    )
    scheduled_request = operation_request(
        tenant,
        StartCustomConnectorJobRequest(
            connector_id=connector.connector_id,
            job_configuration=SecretStr("{}"),
        ),
        idempotency_key="2026-10-10T03:04:05.000000+00:00",
    )

    first = activity.start(scheduled_request).job
    stored = jobs.get_job(GetJobRequest(tenant, connector.connector_id, first.job_id))
    assert stored.encrypted_configuration != b"{}"
    fetched = GetCustomConnectorJobActivity(jobs, codec).fetch(
        operation_request(
            tenant,
            GetCustomConnectorJobRequest(
                connector_id=connector.connector_id,
                job_id=first.job_id,
            ),
        )
    )
    assert fetched.job.job_configuration.get_secret_value() == "{}"

    active_replay = activity.start(scheduled_request).job
    jobs.update_job_status(
        UpdateJobStatusRequest(
            tenant,
            connector.connector_id,
            first.job_id,
            JobStatus.SUCCEEDED,
        )
    )
    terminal_replay = activity.start(scheduled_request).job

    assert active_replay.job_id == first.job_id
    assert active_replay.status is JobStatus.STARTED
    assert terminal_replay.job_id == first.job_id
    assert terminal_replay.status is JobStatus.SUCCEEDED


@mock_aws
def test_get_connector_uses_request_context_endpoint_when_not_configured() -> None:
    connectors, _ = create_tables()
    codec = FakeConfigurationCodec()
    tenant = TenantContext("123456789012", "us-east-1")
    connector = (
        create_connector_activity(
            connectors,
            ConfigurationValidator(),
            codec,
            MagicMock(),
        )
        .create(operation_request(tenant, create_payload()))
        .connector
    )
    activity = GetCustomConnectorActivity(
        connectors,
        codec,
        ServiceReferenceConfiguration(
            account_id="999999999999",
            endpoint="",
            region="us-west-2",
            partition="aws-us-gov",
            service_principal="ccf.amazonaws.com",
            ingestion_bucket_name="ingestion-bucket",
            destination_mode=DestinationMode.S3,
        ),
    )

    response = activity.fetch(
        operation_request(
            tenant,
            GetCustomConnectorRequest(connector_id=connector.connector_id),
            service_endpoint="https://trusted.execute-api.us-east-1.amazonaws.com/prod",
        )
    )

    assert response.service_reference.endpoint == ("https://trusted.execute-api.us-east-1.amazonaws.com/prod")
    assert response.service_reference.service_account_id == "999999999999"
    assert response.service_reference.region == "us-west-2"
    assert response.service_reference.service_principal == "ccf.amazonaws.com"


@pytest.mark.parametrize(
    ("image_account_id", "source_account_id"),
    [
        ("999999999999", "123456789012"),
        ("123456789012", "999999999999"),
        ("999999999999", "999999999999"),
    ],
    ids=["foreign-image", "foreign-source-role", "both-foreign"],
)
def test_create_rejects_identities_outside_the_authenticated_tenant(
    image_account_id: str,
    source_account_id: str,
) -> None:
    tenant = TenantContext("123456789012", "us-east-1")
    connectors = MagicMock()
    runtime_roles = runtime_roles_double()
    payload = create_payload().model_copy(
        update={
            "container_properties": create_payload().container_properties.model_copy(
                update={"image_uri": (f"{image_account_id}.dkr.ecr.us-east-1.amazonaws.com/connector:latest")}
            ),
            "source_access": SourceAccessConfiguration(
                role_arn=f"arn:aws:iam::{source_account_id}:role/CcfSourceRole-connector",
                external_id="external-id",
            ),
        }
    )

    with pytest.raises(ConfigurationValidationError, match="calling account"):
        create_connector_activity(
            connectors,
            ConfigurationValidator(),
            FakeConfigurationCodec(),
            MagicMock(),
            runtime_roles,
        ).create(operation_request(tenant, payload))

    connectors.create_connector.assert_not_called()
    runtime_roles.create.assert_not_called()


def test_update_rejects_a_source_role_outside_the_authenticated_tenant() -> None:
    tenant = TenantContext("123456789012", "us-east-1")
    connectors = MagicMock()
    runtime_roles = runtime_roles_double()

    with pytest.raises(ConfigurationValidationError, match="calling account"):
        update_connector_activity(
            connectors,
            ConfigurationValidator(),
            FakeConfigurationCodec(),
            MagicMock(),
            runtime_roles,
        ).update(
            operation_request(
                tenant,
                UpdateCustomConnectorRequest(
                    connector_id="cc-1",
                    source_access=SourceAccessConfiguration(
                        role_arn="arn:aws:iam::999999999999:role/CcfSourceRole-connector",
                        external_id="external-id",
                    ),
                ),
            )
        )

    connectors.update_connector.assert_not_called()
    runtime_roles.replace_policy.assert_not_called()


@mock_aws
def test_create_persists_the_runtime_role_arn_scoped_to_the_exact_source_role() -> None:
    connectors, _ = create_tables()
    tenant = TenantContext("123456789012", "us-east-1")
    runtime_roles = runtime_roles_double()

    connector = (
        create_connector_activity(
            connectors,
            ConfigurationValidator(),
            FakeConfigurationCodec(),
            MagicMock(),
            runtime_roles,
        )
        .create(operation_request(tenant, create_payload()))
        .connector
    )

    runtime_request = runtime_roles.create.call_args.args[0]
    assert runtime_request.identity.connector_id == connector.connector_id
    assert runtime_request.identity.tenant == tenant
    assert runtime_request.source_role_arn == "arn:aws:iam::123456789012:role/CcfSourceRole-connector"
    stored = connectors.get_connector(GetConnectorRequest(tenant, connector.connector_id))
    assert stored.runtime_role_arn == _RUNTIME_ROLE_ARN


@mock_aws
def test_record_write_failure_deletes_the_new_runtime_role() -> None:
    connectors, _ = create_tables()
    tenant = TenantContext("123456789012", "us-east-1")
    runtime_roles = runtime_roles_double()

    with (
        patch.object(
            connectors,
            "create_connector",
            side_effect=StorageOperationError("connector write failed"),
        ),
        pytest.raises(StorageOperationError, match="connector write failed"),
    ):
        create_connector_activity(
            connectors,
            ConfigurationValidator(),
            FakeConfigurationCodec(),
            MagicMock(),
            runtime_roles,
        ).create(operation_request(tenant, create_payload()))

    runtime_roles.delete.assert_called_once()
    assert runtime_roles.delete.call_args.args[0].tenant == tenant


@mock_aws
def test_create_schedule_failure_also_deletes_the_runtime_role() -> None:
    connectors, _ = create_tables()
    tenant = TenantContext("123456789012", "us-east-1")
    runtime_roles = runtime_roles_double()
    schedule_lifecycle = MagicMock()
    schedule_lifecycle.create.side_effect = RuntimeError("schedule create failed")
    payload = create_payload().model_copy(update={"schedule": connector_schedule()})

    with pytest.raises(RuntimeError, match="schedule create failed"):
        create_connector_activity(
            connectors,
            ConfigurationValidator(),
            FakeConfigurationCodec(),
            schedule_lifecycle,
            runtime_roles,
        ).create(operation_request(tenant, payload))

    runtime_roles.delete.assert_called_once()
    connector_id = runtime_roles.delete.call_args.args[0].connector_id
    with pytest.raises(StorageNotFoundError):
        connectors.get_connector(GetConnectorRequest(tenant, connector_id))


@mock_aws
def test_delete_revokes_the_runtime_role_before_removing_the_record() -> None:
    connectors, _ = create_tables()
    tenant = TenantContext("123456789012", "us-east-1")
    runtime_roles = runtime_roles_double()
    connector = (
        create_connector_activity(
            connectors,
            ConfigurationValidator(),
            FakeConfigurationCodec(),
            MagicMock(),
        )
        .create(operation_request(tenant, create_payload()))
        .connector
    )

    delete_connector_activity(connectors, MagicMock(), runtime_roles).delete(
        operation_request(tenant, DeleteCustomConnectorRequest(connector_id=connector.connector_id))
    )

    runtime_roles.delete.assert_called_once()
    assert runtime_roles.delete.call_args.args[0].connector_id == connector.connector_id
    with pytest.raises(StorageNotFoundError):
        connectors.get_connector(GetConnectorRequest(tenant, connector.connector_id))


@mock_aws
def test_record_delete_failure_restores_the_runtime_role() -> None:
    connectors, _ = create_tables()
    tenant = TenantContext("123456789012", "us-east-1")
    runtime_roles = runtime_roles_double()
    connector = (
        create_connector_activity(
            connectors,
            ConfigurationValidator(),
            FakeConfigurationCodec(),
            MagicMock(),
        )
        .create(operation_request(tenant, create_payload()))
        .connector
    )

    with (
        patch.object(
            connectors,
            "delete_connector",
            side_effect=StorageOperationError("connector delete failed"),
        ),
        pytest.raises(StorageOperationError, match="connector delete failed"),
    ):
        delete_connector_activity(connectors, MagicMock(), runtime_roles).delete(
            operation_request(tenant, DeleteCustomConnectorRequest(connector_id=connector.connector_id))
        )

    restore_request = runtime_roles.create.call_args.args[0]
    assert restore_request.identity.connector_id == connector.connector_id
    assert restore_request.source_role_arn == "arn:aws:iam::123456789012:role/CcfSourceRole-connector"


@mock_aws
def test_update_replaces_the_runtime_policy_and_restores_it_on_failure() -> None:
    connectors, _ = create_tables()
    tenant = TenantContext("123456789012", "us-east-1")
    validator = ConfigurationValidator()
    codec = FakeConfigurationCodec()
    connector = (
        create_connector_activity(connectors, validator, codec, MagicMock())
        .create(operation_request(tenant, create_payload()))
        .connector
    )
    replacement_source = SourceAccessConfiguration(
        role_arn="arn:aws:iam::123456789012:role/CcfSourceRole-replacement",
        external_id="external-id",
    )
    runtime_roles = runtime_roles_double()
    runtime_roles.replace_policy.side_effect = [RuntimeRoleError("policy update failed"), None]

    with pytest.raises(RuntimeRoleError, match="policy update failed"):
        update_connector_activity(connectors, validator, codec, MagicMock(), runtime_roles).update(
            operation_request(
                tenant,
                UpdateCustomConnectorRequest(
                    connector_id=connector.connector_id,
                    source_access=replacement_source,
                ),
            )
        )

    assert runtime_roles.replace_policy.call_count == 2
    restore_request = runtime_roles.replace_policy.call_args_list[1].args[0]
    assert restore_request.source_role_arn == "arn:aws:iam::123456789012:role/CcfSourceRole-connector"
    stored = connectors.get_connector(GetConnectorRequest(tenant, connector.connector_id))
    assert stored.source_access.role_arn == "arn:aws:iam::123456789012:role/CcfSourceRole-connector"


@mock_aws
def test_destination_prefix_isolates_two_connectors_in_the_same_tenant() -> None:
    connectors, _ = create_tables()
    tenant = TenantContext("123456789012", "us-east-1")
    other_tenant = TenantContext("999999999999", "us-east-1")
    codec = FakeConfigurationCodec()
    validator = ConfigurationValidator()
    service_reference_configuration = ServiceReferenceConfiguration(
        account_id="210987654321",
        endpoint="https://ccf.example.com/prod",
        region="us-east-1",
        partition="aws",
        service_principal="ccf.amazonaws.com",
        ingestion_bucket_name="ccf-ingestion",
        destination_mode=DestinationMode.S3,
    )
    activity = GetCustomConnectorActivity(connectors, codec, service_reference_configuration)
    prefixes: list[str] = []
    for owner, configuration in ((tenant, '{"repository":"one"}'), (tenant, '{"repository":"two"}')):
        connector = (
            create_connector_activity(connectors, validator, codec, MagicMock())
            .create(operation_request(owner, create_payload(configuration)))
            .connector
        )
        response = activity.fetch(
            operation_request(owner, GetCustomConnectorRequest(connector_id=connector.connector_id))
        )
        prefix = response.service_reference.destination.object_prefix
        assert prefix == f"documents/{owner.account_id}/{connector.connector_id}/"
        prefixes.append(prefix or "")

    assert prefixes[0] != prefixes[1]
    assert all(prefix.startswith("documents/123456789012/") for prefix in prefixes)
    assert not any(prefix.startswith(f"documents/{other_tenant.account_id}/") for prefix in prefixes)


@mock_aws
def test_create_without_a_serving_rest_api_id_fails_closed() -> None:
    connectors, _ = create_tables()
    tenant = TenantContext("123456789012", "us-east-1")
    runtime_roles = runtime_roles_double()

    with pytest.raises(RuntimeRoleError, match="serving REST API id"):
        create_connector_activity(
            connectors,
            ConfigurationValidator(),
            FakeConfigurationCodec(),
            MagicMock(),
            runtime_roles,
        ).create(operation_request(tenant, create_payload(), rest_api_id=None))

    runtime_roles.create.assert_not_called()


def test_runtime_policy_requires_a_well_formed_rest_api_id() -> None:
    tenant = TenantContext("123456789012", "us-east-1")

    for rest_api_id in ("", "not valid", "abc/def"):
        with pytest.raises(RuntimeRoleError, match="serving REST API id"):
            RuntimeRolePolicyRequest(
                identity=RuntimeRoleIdentity(tenant, "cc-1"),
                source_role_arn="arn:aws:iam::123456789012:role/CcfSourceRole-connector",
                rest_api_id=rest_api_id,
            )


@pytest.mark.parametrize(
    "source_role_arn",
    [
        "arn:aws:iam::123456789012:role/source",
        "arn:aws:iam::123456789012:role/team/CcfSourceRole-connector",
    ],
    ids=["unshaped-name", "iam-path"],
)
def test_create_rejects_source_roles_the_shared_boundary_denies(source_role_arn: str) -> None:
    tenant = TenantContext("123456789012", "us-east-1")
    connectors = MagicMock()
    runtime_roles = runtime_roles_double()
    payload = create_payload().model_copy(
        update={"source_access": SourceAccessConfiguration(role_arn=source_role_arn, external_id="external-id")}
    )

    with pytest.raises(ConfigurationValidationError):
        create_connector_activity(
            connectors,
            ConfigurationValidator(),
            FakeConfigurationCodec(),
            MagicMock(),
            runtime_roles,
        ).create(operation_request(tenant, payload))

    connectors.create_connector.assert_not_called()
    runtime_roles.create.assert_not_called()


def test_create_rejects_a_source_role_outside_configured_client_accounts() -> None:
    tenant = TenantContext("555555555555", "us-east-1")
    connectors = MagicMock()
    runtime_roles = runtime_roles_double()
    payload = create_payload().model_copy(
        update={
            "container_properties": create_payload().container_properties.model_copy(
                update={"image_uri": "555555555555.dkr.ecr.us-east-1.amazonaws.com/connector:latest"}
            ),
            "source_access": SourceAccessConfiguration(
                role_arn="arn:aws:iam::555555555555:role/CcfSourceRole-connector",
                external_id="external-id",
            ),
        }
    )

    with pytest.raises(ConfigurationValidationError, match="configured client account"):
        create_connector_activity(
            connectors,
            ConfigurationValidator(),
            FakeConfigurationCodec(),
            MagicMock(),
            runtime_roles,
        ).create(operation_request(tenant, payload))

    runtime_roles.create.assert_not_called()


@mock_aws
def test_source_access_cannot_change_while_the_connector_is_in_use() -> None:
    connectors, _ = create_tables()
    codec = FakeConfigurationCodec()
    validator = ConfigurationValidator()
    tenant = TenantContext("123456789012", "us-east-1")
    connector = (
        create_connector_activity(connectors, validator, codec, MagicMock())
        .create(operation_request(tenant, create_payload()))
        .connector
    )
    connectors.update_connector_status(
        UpdateConnectorStatusRequest(tenant, connector.connector_id, ConnectorStatus.IN_USE, "job-1")
    )
    runtime_roles = runtime_roles_double()

    with pytest.raises(StorageConflictError, match="source access cannot change"):
        update_connector_activity(connectors, validator, codec, MagicMock(), runtime_roles).update(
            operation_request(
                tenant,
                UpdateCustomConnectorRequest(
                    connector_id=connector.connector_id,
                    source_access=SourceAccessConfiguration(
                        role_arn="arn:aws:iam::123456789012:role/CcfSourceRole-replacement",
                        external_id="external-id",
                    ),
                ),
            )
        )

    # The live execution keeps the source permission it started with.
    stored = connectors.get_connector(GetConnectorRequest(tenant, connector.connector_id))
    assert stored.source_access.role_arn == "arn:aws:iam::123456789012:role/CcfSourceRole-connector"
    runtime_roles.replace_policy.assert_not_called()


@mock_aws
def test_delete_claim_blocks_a_later_job_reservation() -> None:
    connectors, jobs = create_tables()
    tenant = TenantContext("123456789012", "us-east-1")
    connector = (
        create_connector_activity(connectors, ConfigurationValidator(), FakeConfigurationCodec(), MagicMock())
        .create(operation_request(tenant, create_payload()))
        .connector
    )

    connectors.claim_connector_deletion(ClaimConnectorDeletionRequest(tenant, connector.connector_id))

    # StartCustomConnectorJob must not reserve a connector whose deletion began.
    with pytest.raises(StorageConflictError):
        jobs.start_job(StartJobRequest(tenant, connector.connector_id, b"configuration"))
    with pytest.raises(StorageConflictError):
        connectors.update_connector_status(
            UpdateConnectorStatusRequest(tenant, connector.connector_id, ConnectorStatus.IN_USE, "job-1")
        )


@mock_aws
def test_delete_claim_rejects_an_in_use_connector_before_revoking_anything() -> None:
    connectors, _ = create_tables()
    tenant = TenantContext("123456789012", "us-east-1")
    runtime_roles = runtime_roles_double()
    schedule_lifecycle = MagicMock()
    connector = (
        create_connector_activity(connectors, ConfigurationValidator(), FakeConfigurationCodec(), MagicMock())
        .create(operation_request(tenant, create_payload()))
        .connector
    )
    connectors.update_connector_status(
        UpdateConnectorStatusRequest(tenant, connector.connector_id, ConnectorStatus.IN_USE, "job-1")
    )

    with pytest.raises(StorageConflictError, match="Connector is in use"):
        delete_connector_activity(connectors, schedule_lifecycle, runtime_roles).delete(
            operation_request(tenant, DeleteCustomConnectorRequest(connector_id=connector.connector_id))
        )

    runtime_roles.delete.assert_not_called()
    schedule_lifecycle.delete.assert_not_called()


@mock_aws
def test_schedule_delete_failure_restores_the_runtime_role_and_releases_the_claim() -> None:
    connectors, jobs = create_tables()
    tenant = TenantContext("123456789012", "us-east-1")
    runtime_roles = runtime_roles_double()
    schedule_lifecycle = MagicMock()
    schedule_lifecycle.delete.side_effect = RuntimeError("schedule delete failed")
    payload = create_payload().model_copy(update={"schedule": connector_schedule()})
    connector = (
        create_connector_activity(connectors, ConfigurationValidator(), FakeConfigurationCodec(), MagicMock())
        .create(operation_request(tenant, payload))
        .connector
    )

    with pytest.raises(RuntimeError, match="schedule delete failed"):
        delete_connector_activity(connectors, schedule_lifecycle, runtime_roles).delete(
            operation_request(tenant, DeleteCustomConnectorRequest(connector_id=connector.connector_id))
        )

    # The connector record survives, so it must keep its runtime role.
    stored = connectors.get_connector(GetConnectorRequest(tenant, connector.connector_id))
    assert stored.runtime_role_arn == _RUNTIME_ROLE_ARN
    runtime_roles.delete.assert_called_once()
    restore_request = runtime_roles.create.call_args.args[0]
    assert restore_request.source_role_arn == "arn:aws:iam::123456789012:role/CcfSourceRole-connector"
    schedule_lifecycle.create.assert_called_once()
    # The released claim lets a new job reserve the connector again.
    jobs.start_job(StartJobRequest(tenant, connector.connector_id, b"configuration"))


@mock_aws
def test_record_delete_failure_restores_role_schedule_and_releases_the_claim() -> None:
    connectors, jobs = create_tables()
    tenant = TenantContext("123456789012", "us-east-1")
    runtime_roles = runtime_roles_double()
    schedule_lifecycle = MagicMock()
    payload = create_payload().model_copy(update={"schedule": connector_schedule()})
    connector = (
        create_connector_activity(connectors, ConfigurationValidator(), FakeConfigurationCodec(), MagicMock())
        .create(operation_request(tenant, payload))
        .connector
    )

    with (
        patch.object(connectors, "delete_connector", side_effect=StorageOperationError("record delete failed")),
        pytest.raises(StorageOperationError, match="record delete failed"),
    ):
        delete_connector_activity(connectors, schedule_lifecycle, runtime_roles).delete(
            operation_request(tenant, DeleteCustomConnectorRequest(connector_id=connector.connector_id))
        )

    runtime_roles.create.assert_called_once()
    schedule_lifecycle.create.assert_called_once()
    assert schedule_lifecycle.create.call_args.args[0].schedule == connector_schedule()
    jobs.start_job(StartJobRequest(tenant, connector.connector_id, b"configuration"))


@mock_aws
def test_successful_delete_removes_the_record_and_its_runtime_role() -> None:
    connectors, _ = create_tables()
    tenant = TenantContext("123456789012", "us-east-1")
    runtime_roles = runtime_roles_double()
    connector = (
        create_connector_activity(connectors, ConfigurationValidator(), FakeConfigurationCodec(), MagicMock())
        .create(operation_request(tenant, create_payload()))
        .connector
    )

    delete_connector_activity(connectors, MagicMock(), runtime_roles).delete(
        operation_request(tenant, DeleteCustomConnectorRequest(connector_id=connector.connector_id))
    )

    runtime_roles.delete.assert_called_once()
    runtime_roles.create.assert_not_called()
    with pytest.raises(StorageNotFoundError):
        connectors.get_connector(GetConnectorRequest(tenant, connector.connector_id))
