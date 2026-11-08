"""`ccf-connector run <module>:<class>`: the composition root of every job container.

It reads the six variables the job definition and the job state machine set, makes JSON lines on
stdout the root logger's only output, builds the runtime role session and from it only the
framework API client and the STS client, loads the implementation class, and runs the job. The
process exits with the runner's `JobExitCode`, which the framework turns into the job's
`status_reason`.
"""

from __future__ import annotations

import argparse
import importlib
import logging
import sys
import time
from collections.abc import Sequence
from typing import Annotated

import boto3
from ccf_api_models.identifiers import Region
from ccf_api_models.job_container import JobExitCode, RuntimeVariable
from ccf_api_models.models import ConnectorId, DestinationId, JobId, TenantArnPrefix
from pydantic import Field, StringConstraints, TypeAdapter, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from custom_connector_framework.client import CcfClientSettings, RetrySettings, TenantDelegation, create_ccf_client
from custom_connector_framework.client.pages import Pages
from custom_connector_framework.client.send_deadline import SendDeadline
from custom_connector_framework.implementation import ConnectorImplementation
from custom_connector_framework.log_lines import InstallLogLinesRequest, JobLogFields, install_log_lines
from custom_connector_framework.runner.file_sweeper import FileSweeper
from custom_connector_framework.runner.job_inputs_reader import JobIdentity, JobInputsReader
from custom_connector_framework.runner.job_runner import JobRunner
from custom_connector_framework.runner.secret_reader import SecretReader
from custom_connector_framework.runner.source_access import SourceAccess
from custom_connector_framework.runner.termination_signal import TerminationSignal

logger = logging.getLogger(__name__)

UNEXPECTED_FAILURE_EXIT_CODE = 1
"""A toolkit failure outside the table; the job's `status_reason` is then Batch's reason."""
JOB_RETRY_SETTINGS = RetrySettings(attempts=10, base_seconds=0.5, cap_seconds=20.0)
"""Jobs share the API's stage-wide rate limit, so a job retries a throttled call for up to about two minutes
before it fails, instead of the tenant default's few seconds."""


class RuntimeSettings(BaseSettings):
    """The six variables set on the `connector` container; none has a default."""

    model_config = SettingsConfigDict(frozen=True, extra="forbid", case_sensitive=False)

    api_endpoint: Annotated[str, StringConstraints(pattern=r"^https://[^\s/]+(/[^\s]*)?$")] = Field(
        validation_alias=RuntimeVariable.API_ENDPOINT.value
    )
    region: Region = Field(validation_alias=RuntimeVariable.REGION.value)
    tenant_arn_prefix: TenantArnPrefix = Field(validation_alias=RuntimeVariable.TENANT_ARN_PREFIX.value)
    destination_id: DestinationId = Field(validation_alias=RuntimeVariable.DESTINATION_ID.value)
    connector_id: ConnectorId = Field(validation_alias=RuntimeVariable.CONNECTOR_ID.value)
    job_id: JobId = Field(validation_alias=RuntimeVariable.JOB_ID.value)

    def job_identity(self) -> JobIdentity:
        return JobIdentity(
            tenant_arn_prefix=self.tenant_arn_prefix,
            region=self.region,
            destination_id=self.destination_id,
            connector_id=self.connector_id,
            job_id=self.job_id,
        )


class ImplementationLoadError(Exception):
    """`<module>:<class>` doesn't name an importable `ConnectorImplementation` subclass."""


_IMPLEMENTATION_CLASS = TypeAdapter(type[ConnectorImplementation])


class ImplementationLoader:
    """Imports `<module>:<class>` and checks that it subclasses `ConnectorImplementation`."""

    def load(self, target: str) -> type[ConnectorImplementation]:
        module_name, _, class_name = target.partition(":")
        if not module_name or not class_name:
            raise ImplementationLoadError("The target must be <module>:<class>")
        try:
            module = importlib.import_module(module_name)
        except Exception as error:
            # A module can fail to import with any exception its own top-level code raises.
            raise ImplementationLoadError("The target's module failed to import") from error
        try:
            return _IMPLEMENTATION_CLASS.validate_python(getattr(module, class_name))
        except (AttributeError, ValidationError) as error:
            raise ImplementationLoadError("The target doesn't name a ConnectorImplementation subclass") from error


class JobLauncher:
    """Builds the job's runner from the runtime role session, which builds only the API and STS clients."""

    def __init__(self, runtime_session: boto3.Session) -> None:
        self._runtime_session = runtime_session

    def runner(self, settings: RuntimeSettings) -> JobRunner:
        send_deadline = SendDeadline(time.monotonic)
        api = create_ccf_client(
            CcfClientSettings(
                endpoint=settings.api_endpoint,
                region=settings.region,
                session=self._runtime_session,
                delegation=TenantDelegation(
                    tenant_arn_prefix=settings.tenant_arn_prefix, connector_id=settings.connector_id
                ),
                correlation_id=settings.job_id,
                send_deadline=send_deadline,
                retry=JOB_RETRY_SETTINGS,
            )
        )
        pages = Pages(api)
        return JobRunner(
            settings.job_identity(),
            inputs=JobInputsReader(api),
            source_access=SourceAccess(self._runtime_session.client("sts"), boto3.Session),
            secrets=SecretReader(),
            api=api,
            pages=pages,
            sweeper=FileSweeper(pages),
            termination=TerminationSignal(),
            send_deadline=send_deadline,
        )


def _arguments(argv: Sequence[str] | None) -> str:
    parser = argparse.ArgumentParser(prog="ccf-connector")
    subcommands = parser.add_subparsers(dest="command", required=True)
    run_parser = subcommands.add_parser("run", help="run one job of a connector implementation")
    run_parser.add_argument("implementation", help="<module>:<class> of a ConnectorImplementation subclass")
    target: str = parser.parse_args(argv).implementation
    return target


def _settings() -> RuntimeSettings | None:
    """The settings, with log lines installed with the job's fields; None, logged by field name, when they fail."""
    try:
        settings = RuntimeSettings()
    except ValidationError as error:
        install_log_lines(InstallLogLinesRequest(fields=JobLogFields(), stream=sys.stdout))
        # Names the variables at fault, never their values.
        fields = sorted({".".join(str(part) for part in detail["loc"]) for detail in error.errors()})
        logger.error("runtime_settings_invalid", extra={"fields": fields})
        return None
    install_log_lines(
        InstallLogLinesRequest(
            fields=JobLogFields(job_id=settings.job_id, connector_id=settings.connector_id), stream=sys.stdout
        )
    )
    return settings


def _run_job(target: str) -> JobExitCode:
    settings = _settings()
    if settings is None:
        return JobExitCode.CONNECTOR_IMPLEMENTATION_ERROR
    try:
        implementation = ImplementationLoader().load(target)
    except ImplementationLoadError:
        logger.exception("implementation_not_loaded")
        return JobExitCode.CONNECTOR_IMPLEMENTATION_ERROR
    launcher = JobLauncher(boto3.Session(region_name=settings.region))
    return launcher.runner(settings).run(implementation).exit_code


def main(argv: Sequence[str] | None = None) -> int:
    """Run one job and return its exit code; the console script exits with it."""
    target = _arguments(argv)
    try:
        return int(_run_job(target))
    except Exception:
        logger.exception("job_failed")
        return UNEXPECTED_FAILURE_EXIT_CODE
