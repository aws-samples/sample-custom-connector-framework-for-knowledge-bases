"""Typed settings for each Lambda composition root, read once from the environment at cold start.

The framework CDK app writes every value, so none has a default: a missing or malformed value
fails the cold start with a `ValidationError` that names the field. Groups are nested models,
so `tables.destinations` is `TABLES__DESTINATIONS`; `contracts/settings.json` lists each root's
names. No setting holds a secret, so a composition root may log them all.
"""

from __future__ import annotations

from typing import Annotated

from ccf_api_models.identifiers import AccountId, Partition, Region, TenantContext
from pydantic import BaseModel, ConfigDict, Field, Json, StringConstraints, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from common.runtime_role import RuntimeRoute

type Arn = Annotated[str, StringConstraints(pattern=r"^arn:aws(-us-gov|-cn|-eusc)?:[a-z0-9-]+:")]
type StateMachineArn = Annotated[
    str, StringConstraints(pattern=r"^arn:aws(-us-gov|-cn|-eusc)?:states:[a-z0-9-]+:[0-9]{12}:stateMachine:")
]
type TableName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{3,255}$")]
type HttpsEndpoint = Annotated[str, StringConstraints(pattern=r"^https://[^\s/]+(/[^\s]*)?$")]
type ImageReference = Annotated[str, StringConstraints(pattern=r"^[^\s:@]+:[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")]
"""An image with a version tag; the framework never runs `latest` or an untagged image."""


class _SettingsGroup(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TableNames(_SettingsGroup):
    destinations: TableName
    connectors: TableName
    jobs: TableName
    collections: TableName
    documents: TableName
    idempotency: TableName


class IdempotencySettings(_SettingsGroup):
    """How long a create's saved response answers a retry; the table is `tables.idempotency`."""

    expiry_seconds: int = Field(ge=60)


class RuntimeRoleSettings(_SettingsGroup):
    """The framework values every runtime role's policy names, and the role's own names."""

    rest_api_id: Annotated[str, StringConstraints(pattern=r"^[a-z0-9]{10}$")]
    stage_name: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,128}$")]
    boundary_arn: Arn
    routes: Json[Annotated[tuple[RuntimeRoute, ...], Field(min_length=1)]]
    """The routes whose `x-ccf-callers` holds `RUNTIME`, as JSON."""
    role_path: Annotated[str, StringConstraints(pattern=r"^/[A-Za-z0-9_-]+/$")]
    role_name_prefix: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9+=,.@_-]{1,32}$")]
    policy_name: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9+=,.@_-]{1,128}$")]
    max_session_seconds: int = Field(ge=3_600, le=43_200)
    """IAM's range for a role's maximum session duration."""


class SchedulerSettings(_SettingsGroup):
    """Where every custom connector schedule delivers, and how its failed deliveries retry."""

    target_arn: Arn
    role_arn: Arn
    group_name: str = Field(min_length=1, max_length=64)
    dead_letter_arn: Arn
    retry_attempts: int = Field(ge=0, le=185)
    max_event_age_seconds: int = Field(ge=60, le=86400)


class WorkflowArns(_SettingsGroup):
    destination: StateMachineArn
    """`CcfDestinationWorkflow`, also the external ID every `CcfDestinationRole` trust policy requires."""
    connector_deletion: StateMachineArn


class JobSettings(_SettingsGroup):
    """The job state machine and how long the jobs table keeps a job record."""

    job_execution_arn: StateMachineArn
    """`CcfJobExecution`, which runs each job."""
    record_retention_seconds: int = Field(ge=1)
    """How long a finished job's record stays."""
    active_record_retention_seconds: int = Field(ge=1)
    """How long a started job's record stays: the finished retention plus the longest job timeout."""


class JobDefinitionSettings(_SettingsGroup):
    """What every custom connector's Batch job definition is built from."""

    name_prefix: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,32}$")]
    execution_role_arn: Arn
    """The role ECS uses to pull the connector image and write the log router's logs."""
    api_endpoint: HttpsEndpoint
    """The framework API endpoint each job calls."""
    log_router_image: ImageReference
    """The `aws-for-fluent-bit` image every job definition runs next to the connector container."""
    log_router_log_group: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_./#-]{1,512}$")]
    log_router_stream_prefix: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,64}$")]


class HandlerSettings(BaseSettings):
    """Values every root reads: where it runs, its tables, and the key of its stored resource data."""

    model_config = SettingsConfigDict(frozen=True, extra="ignore", case_sensitive=False, env_nested_delimiter="__")

    aws_region: Region
    """The service Region; Lambda sets `AWS_REGION`."""
    aws_partition: Partition
    service_account_id: AccountId
    tables: TableNames
    resource_data_key_arn: Arn

    @property
    def service(self) -> TenantContext:
        """The service account, in the service Region and partition."""
        return TenantContext(partition=self.aws_partition, region=self.aws_region, account_id=self.service_account_id)


class ApiSettings(HandlerSettings):
    aws_url_suffix: Annotated[str, StringConstraints(pattern=r"^[a-z0-9.-]+$")]
    """The partition's DNS suffix, for example `amazonaws.com`; image URIs must use it."""
    idempotency: IdempotencySettings
    runtime_roles: RuntimeRoleSettings
    scheduler: SchedulerSettings
    workflows: WorkflowArns
    jobs: JobSettings
    job_definitions: JobDefinitionSettings
    allowed_tenant_account_ids: Annotated[tuple[AccountId, ...], NoDecode]
    """Comma-separated; empty allows only the service account."""

    @field_validator("allowed_tenant_account_ids", mode="before")
    @classmethod
    def split_account_ids(cls, value: str) -> tuple[str, ...]:
        """Read the comma-separated account list the CDK app writes."""
        return tuple(account_id.strip() for account_id in value.split(",") if account_id.strip())


class ScheduledStartSettings(HandlerSettings):
    idempotency: IdempotencySettings
    jobs: JobSettings


class IngestionConsumerSettings(HandlerSettings):
    ingestion_dead_letter_queue_url: HttpsEndpoint
    workflows: WorkflowArns
