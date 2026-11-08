"""The settings every `connector_ops` Lambda reads: where the framework API is."""

from ccf_api_models.identifiers import Region
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConnectorOpsSettings(BaseSettings):
    """The framework API the custom resources call, set by the construct that creates the Lambda."""

    model_config = SettingsConfigDict(frozen=True)

    framework_endpoint: str
    service_region: Region
