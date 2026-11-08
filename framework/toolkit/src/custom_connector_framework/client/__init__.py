"""The framework API client: `create_ccf_client` builds a `CcfApi` whose one `call` takes any operation's request."""

from ccf_api_models.operations import CcfApi

from custom_connector_framework.client.ccf_client import CcfClient, create_ccf_client
from custom_connector_framework.client.settings import CcfClientSettings, RetrySettings, TenantDelegation
from custom_connector_framework.client.transport import ApiFailure, CcfApiError

__all__ = [
    "ApiFailure",
    "CcfApi",
    "CcfApiError",
    "CcfClient",
    "CcfClientSettings",
    "RetrySettings",
    "TenantDelegation",
    "create_ccf_client",
]
