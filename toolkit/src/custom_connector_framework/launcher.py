"""Generic console launcher for framework-owned connector jobs."""

from __future__ import annotations

import argparse
import importlib
import os
from collections.abc import Mapping, Sequence
from typing import Any, cast

import boto3
from ccf_api_models.models import (
    DeploymentDestination,
    DestinationMode,
    GetCustomConnectorJobRequest,
    GetCustomConnectorRequest,
    SourceAccessConfiguration,
)
from pydantic import BaseModel, ConfigDict, ValidationError

from custom_connector_framework.client import CcfClient, _create_runtime_client
from custom_connector_framework.definition import ConnectorContext, ConnectorDefinition
from custom_connector_framework.interface import ConnectorBinding, DocumentSynchronizer, SyncResult
from custom_connector_framework.object_store.s3 import S3ObjectStore
from custom_connector_framework.providers.base import KnowledgeBaseProviderAdapter
from custom_connector_framework.providers.bedrock import BedrockKnowledgeBaseAdapter
from custom_connector_framework.providers.quick import QuickKnowledgeBaseAdapter
from custom_connector_framework.providers.s3 import S3Adapter
from custom_connector_framework.sink import DocumentSink, SinkConfig

_ENDPOINT_ENV = "CUSTOM_CONNECTOR_FRAMEWORK_API_ENDPOINT"
_CONNECTOR_ID_ENV = "CUSTOM_CONNECTOR_FRAMEWORK_CUSTOM_CONNECTOR_ID"
_TENANT_ARN_PREFIX_ENV = "CUSTOM_CONNECTOR_FRAMEWORK_CUSTOM_CONNECTOR_ARN_PREFIX"
_JOB_ID_ENV = "CUSTOM_CONNECTOR_JOB_ID"
_REGION_ENV = "AWS_REGION"
_ARN_PART_COUNT = 6


class RuntimeEnvironment(BaseModel):
    """The five service-injected values consumed by the launcher."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    endpoint: str
    connector_id: str
    tenant_arn_prefix: str
    job_id: str
    region: str

    @classmethod
    def from_environ(cls, environment: Mapping[str, str]) -> RuntimeEnvironment:
        """Read only the endpoint, connector ID, tenant prefix, job ID, and Region."""
        try:
            return cls(
                endpoint=environment[_ENDPOINT_ENV],
                connector_id=environment[_CONNECTOR_ID_ENV],
                tenant_arn_prefix=environment[_TENANT_ARN_PREFIX_ENV],
                job_id=environment[_JOB_ID_ENV],
                region=environment[_REGION_ENV],
            )
        except KeyError:
            raise RuntimeError("Required connector runtime configuration is missing.") from None


class SourceSessionRequest(BaseModel):
    """Exact source role and Region needed to create a source-account session."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_access: SourceAccessConfiguration
    region: str


class SourceSessionProvider:
    """Assume one connector's exact source role and vend its boto3 session."""

    def __init__(self, control_session: boto3.Session) -> None:
        self._control_session = control_session

    def assume(self, request: SourceSessionRequest) -> boto3.Session:
        """Assume the registered role with its registered external ID."""
        sts_client = self._control_session.client("sts")
        response = cast(
            dict[str, Any],
            sts_client.assume_role(
                RoleArn=request.source_access.role_arn,
                ExternalId=request.source_access.external_id,
                RoleSessionName="ccf-connector",
            ),
        )
        try:
            credentials = cast(dict[str, str], response["Credentials"])
            return boto3.Session(
                aws_access_key_id=credentials["AccessKeyId"],
                aws_secret_access_key=credentials["SecretAccessKey"],
                aws_session_token=credentials["SessionToken"],
                region_name=request.region,
            )
        except KeyError:
            raise RuntimeError("STS returned an invalid credential response.") from None


class ConnectorLauncher:
    """Load one definition and run it with service-owned wiring."""

    def __init__(
        self,
        environment: RuntimeEnvironment,
        client: CcfClient,
        control_session: boto3.Session,
        source_sessions: SourceSessionProvider,
    ) -> None:
        self._environment = environment
        self._client = client
        self._control_session = control_session
        self._source_sessions = source_sessions

    def run(self, definition_target: str) -> SyncResult:
        """Fetch configuration, create the source session, and sync one snapshot."""
        definition = self._load_definition(definition_target)
        connector_response = self._client.get_custom_connector(
            GetCustomConnectorRequest(connector_id=self._environment.connector_id)
        )
        job_response = self._client.get_custom_connector_job(
            GetCustomConnectorJobRequest(
                connector_id=self._environment.connector_id,
                job_id=self._environment.job_id,
            )
        )
        connector_configuration = self._parse_configuration(
            definition.connector_configuration_model,
            connector_response.connector.connector_configuration.get_secret_value(),
        )
        job_configuration = self._parse_configuration(
            definition.job_configuration_model,
            job_response.job.job_configuration.get_secret_value(),
        )
        source_session = self._source_sessions.assume(
            SourceSessionRequest(
                source_access=connector_response.connector.source_access,
                region=self._environment.region,
            )
        )
        context = ConnectorContext(
            connector_configuration=connector_configuration,
            job_configuration=job_configuration,
            source_session=source_session,
        )
        synchronizer = self._create_synchronizer(connector_response.service_reference.destination)
        return synchronizer.sync(definition.documents(context))

    @staticmethod
    def _load_definition(definition_target: str) -> ConnectorDefinition[BaseModel, BaseModel]:
        module_name, separator, attribute_name = definition_target.partition(":")
        if not separator or not module_name or not attribute_name or ":" in attribute_name:
            raise ValueError("Connector definition must use module:definition syntax.")
        try:
            loaded = getattr(importlib.import_module(module_name), attribute_name)
        except (AttributeError, ImportError):
            raise RuntimeError("Connector definition could not be loaded.") from None
        if not isinstance(loaded, ConnectorDefinition):
            raise TypeError("The imported object must be a ConnectorDefinition.")
        return cast(ConnectorDefinition[BaseModel, BaseModel], loaded)

    @staticmethod
    def _parse_configuration(model_type: type[BaseModel], configuration_json: str) -> BaseModel:
        model_config = model_type.model_config
        if not (
            model_config.get("extra") == "forbid"
            and model_config.get("frozen") is True
            and model_config.get("strict") is True
        ):
            raise TypeError("Connector configuration models must be strict, frozen, and forbid extra fields.")
        try:
            return model_type.model_validate_json(configuration_json)
        except ValidationError:
            raise ValueError("Connector configuration failed validation.") from None

    def _create_synchronizer(self, destination: DeploymentDestination) -> DocumentSynchronizer:
        bucket = self._bucket_name(destination.bucket_arn)
        object_store = S3ObjectStore(self._control_session.client("s3"), bucket)
        sink = DocumentSink(
            object_store=object_store,
            config=SinkConfig(prefix=destination.object_prefix or ""),
            adapter=self._adapter_for(destination.mode),
        )
        return DocumentSynchronizer(
            sink=sink,
            binding=ConnectorBinding(
                connector_id=self._environment.connector_id,
                client=self._client,
            ),
        )

    @staticmethod
    def _adapter_for(mode: DestinationMode) -> KnowledgeBaseProviderAdapter:
        match mode:
            case DestinationMode.S3:
                return S3Adapter()
            case DestinationMode.BEDROCK:
                return BedrockKnowledgeBaseAdapter()
            case DestinationMode.QUICK:
                return QuickKnowledgeBaseAdapter()

    @staticmethod
    def _bucket_name(bucket_arn: str) -> str:
        arn_parts = bucket_arn.split(":", 5)
        if (
            len(arn_parts) != _ARN_PART_COUNT
            or arn_parts[2] != "s3"
            or arn_parts[3]
            or arn_parts[4]
            or not arn_parts[5]
        ):
            raise ValueError("The service destination must contain an S3 bucket ARN.")
        return arn_parts[5]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ccf-connector")
    subcommands = parser.add_subparsers(dest="command", required=True)
    run_parser = subcommands.add_parser("run")
    run_parser.add_argument("definition")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the generic connector console entry point."""
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        environment = RuntimeEnvironment.from_environ(os.environ)
        control_session = boto3.Session(region_name=environment.region)
        client = _create_runtime_client(
            endpoint=environment.endpoint,
            region=environment.region,
            session=control_session,
            tenant_arn_prefix=environment.tenant_arn_prefix,
        )
        launcher = ConnectorLauncher(
            environment=environment,
            client=client,
            control_session=control_session,
            source_sessions=SourceSessionProvider(control_session),
        )
        launcher.run(cast(str, arguments.definition))
    except Exception:
        parser.exit(1, "ccf-connector: connector run failed\n")
    return 0
