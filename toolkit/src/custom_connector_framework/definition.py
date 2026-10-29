"""The complete connector-author contract."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass

import boto3
from pydantic import BaseModel, ConfigDict

from custom_connector_framework.models.document import Document


class EmptyJobConfiguration(BaseModel):
    """Strict empty job configuration shared by connectors without per-run input."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


@dataclass(frozen=True)
class ConnectorContext[ConnectorConfigurationT: BaseModel, JobConfigurationT: BaseModel]:
    """Validated configuration and source-account session for one job."""

    connector_configuration: ConnectorConfigurationT
    job_configuration: JobConfigurationT
    source_session: boto3.Session


@dataclass(frozen=True)
class ConnectorDefinition[ConnectorConfigurationT: BaseModel, JobConfigurationT: BaseModel]:
    """Typed configuration models and the connector's one document factory."""

    connector_configuration_model: type[ConnectorConfigurationT]
    job_configuration_model: type[JobConfigurationT]
    documents: Callable[
        [ConnectorContext[ConnectorConfigurationT, JobConfigurationT]],
        Iterable[Document],
    ]
