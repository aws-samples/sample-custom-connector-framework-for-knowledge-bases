"""The job's inputs from the framework API: the job, its custom connector, and its destination."""

from __future__ import annotations

from botocore.exceptions import BotoCoreError
from ccf_api_models.identifiers import Region
from ccf_api_models.job_container import JobExitCode
from ccf_api_models.models import (
    Connector,
    ConnectorId,
    Destination,
    DestinationId,
    GetCustomConnectorJobRequest,
    GetCustomConnectorRequest,
    GetDestinationRequest,
    Job,
    JobId,
    TenantArnPrefix,
)
from ccf_api_models.operations import CcfApi
from pydantic import BaseModel, ConfigDict

from custom_connector_framework.client import CcfApiError
from custom_connector_framework.runner.exit_codes import JobStartError


class FrameworkApiUnavailableError(JobStartError):
    """A read of the job's inputs failed."""

    exit_code = JobExitCode.FRAMEWORK_API_UNAVAILABLE


class JobIdentity(BaseModel):
    """Who the container runs for: the tenant, the service Region, and the destination, custom connector, and job."""

    model_config = ConfigDict(frozen=True)

    tenant_arn_prefix: TenantArnPrefix
    region: Region
    destination_id: DestinationId
    connector_id: ConnectorId
    job_id: JobId


class JobInputs(BaseModel):
    """What the framework holds for the job."""

    model_config = ConfigDict(frozen=True)

    job: Job
    connector: Connector
    destination: Destination


class JobInputsReader:
    """Reads the three inputs; any failure is `FrameworkApiUnavailableError`."""

    def __init__(self, api: CcfApi) -> None:
        self._api = api

    def read(self, identity: JobIdentity) -> JobInputs:
        try:
            return JobInputs(
                job=self._api.call(
                    GetCustomConnectorJobRequest(
                        destination_id=identity.destination_id,
                        connector_id=identity.connector_id,
                        job_id=identity.job_id,
                    )
                ).job,
                connector=self._api.call(
                    GetCustomConnectorRequest(
                        destination_id=identity.destination_id, connector_id=identity.connector_id
                    )
                ).connector,
                destination=self._api.call(
                    GetDestinationRequest(destination_id=identity.destination_id, connector_id=identity.connector_id)
                ).destination,
            )
        except (CcfApiError, BotoCoreError) as error:
            raise FrameworkApiUnavailableError("The job couldn't read its inputs from the framework API") from error
