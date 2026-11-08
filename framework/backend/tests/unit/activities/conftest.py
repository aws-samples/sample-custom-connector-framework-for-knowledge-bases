"""The activity tests' world: the DAO world seeded with an `ACTIVE` `BEDROCK` destination and a custom connector."""

from __future__ import annotations

from typing import Any

import boto3
import pytest
from ccf_api_models.base import ValueModel
from ccf_api_models.models import ConnectorStatus

from common.observability import MetricRecord, ServiceMetrics
from common.resource_data_codec import ResourceDataCodec
from common.settings import TableNames
from common.tenant_role_clients import AssumeTenantRoleRequest, TenantRoleClients
from tests.unit.dao_world import TENANT_ACCOUNT_ID, DaoTables, DaoWorld

BUCKET = f"ccf-dst-7c41e9a2b5d0-{TENANT_ACCOUNT_ID}-us-east-1"
KNOWLEDGE_BASE_ID = "KB12345678"
DATA_SOURCE_ID = "DS12345678"


class RecordingMetrics(ServiceMetrics):
    """Keeps each metric instead of publishing it."""

    def __init__(self) -> None:
        self.records: list[MetricRecord] = []

    def record(self, metric: MetricRecord) -> None:
        self.records.append(metric)


class RecordingS3:
    """A destination bucket's keys and the calls made on it; `fail_deletes` refuses every key."""

    def __init__(self) -> None:
        self.keys: set[str] = set()
        self.calls: list[str] = []
        self.fail_deletes = False

    def get_paginator(self, name: str) -> RecordingS3:
        return self

    def paginate(self, Bucket: str, Prefix: str) -> list[dict[str, Any]]:  # noqa: N803
        self.calls.append(f"ListObjectsV2 {Bucket} {Prefix}")
        return [{"Contents": [{"Key": key} for key in sorted(self.keys) if key.startswith(Prefix)]}]

    def delete_objects(self, Bucket: str, Delete: dict[str, Any]) -> dict[str, Any]:  # noqa: N803
        self.calls.append(f"DeleteObjects {Bucket}")
        if self.fail_deletes:
            return {"Errors": [{"Key": Delete["Objects"][0]["Key"], "Code": "AccessDenied"}]}
        self.keys -= {entry["Key"] for entry in Delete["Objects"]}
        return {}


class _RecordingSession(boto3.Session):
    def __init__(self, s3: RecordingS3) -> None:
        super().__init__(region_name="us-east-1")
        self._s3 = s3

    def client(self, service_name: str, *_: Any, **__: Any) -> Any:
        assert service_name == "s3"
        return self._s3


class FakeTenantRoleClients(TenantRoleClients):
    """Records each tenant role session asked for; every session's S3 client is the one recording bucket."""

    def __init__(self, s3: RecordingS3) -> None:
        self.s3 = s3
        self.requests: list[AssumeTenantRoleRequest] = []

    def assume(self, request: AssumeTenantRoleRequest) -> boto3.Session:
        self.requests.append(request)
        return _RecordingSession(self.s3)


class ActivitySeed(ValueModel):
    """The custom connector's status, for a test that needs one other than `AVAILABLE`."""

    connector_status: ConnectorStatus = ConnectorStatus.AVAILABLE


class ActivityWorld:
    """The DAOs over moto tables, the recording metrics, bucket, and tenant role clients, and the seeded records."""

    def __init__(self, daos: DaoWorld, seed: ActivitySeed) -> None:
        self.daos = daos
        self.metrics = RecordingMetrics()
        self.s3 = RecordingS3()
        self.tenant_roles = FakeTenantRoleClients(self.s3)
        daos.available_connector()
        destination = daos.destination
        daos.table(daos.names.destinations).update_item(
            Key={"tenant_arn_prefix": destination.tenant.arn_prefix, "destination_id": destination.destination_id},
            UpdateExpression="SET destination_resources = :resources",
            ExpressionAttributeValues={
                ":resources": {
                    "bucket_arn": f"arn:aws:s3:::{BUCKET}",
                    "bucket_name": BUCKET,
                    "knowledge_base_id": KNOWLEDGE_BASE_ID,
                    "data_source_id": DATA_SOURCE_ID,
                }
            },
        )
        if seed.connector_status is not ConnectorStatus.AVAILABLE:
            # An IN_USE custom connector names the job that holds it, as a job start writes.
            daos.table(daos.names.connectors).update_item(
                Key={
                    "tenant_arn_prefix": destination.tenant.arn_prefix,
                    "destination_connector_key": daos.connector.destination_connector_key,
                },
                UpdateExpression="SET #status = :status, active_job_id = :job_id",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":status": seed.connector_status.value, ":job_id": daos.job.job_id},
            )


@pytest.fixture
def activity_seed() -> ActivitySeed:
    """Override in a test module, or parametrize indirectly, to seed another custom connector status."""
    return ActivitySeed()


@pytest.fixture
def activity_world(
    dynamodb: Any, table_names: TableNames, codec: ResourceDataCodec, activity_seed: ActivitySeed
) -> ActivityWorld:
    return ActivityWorld(DaoWorld(DaoTables(resource=dynamodb, names=table_names, codec=codec)), activity_seed)
