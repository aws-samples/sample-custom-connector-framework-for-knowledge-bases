"""What a `CcfClient` needs: the framework's endpoint and Region, a boto3 session, and the runtime's delegation."""

from __future__ import annotations

from typing import Annotated

import boto3
from ccf_api_models.base import ValueModel
from ccf_api_models.identifiers import Region
from ccf_api_models.models import ConnectorId, CorrelationId, TenantArnPrefix
from pydantic import AfterValidator, ConfigDict, Field, StringConstraints

from custom_connector_framework.client.send_deadline import SendDeadline

type HttpsEndpoint = Annotated[
    str,
    StringConstraints(pattern=r"^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[^\s?#]*)?$"),
    AfterValidator(lambda endpoint: endpoint.rstrip("/")),
]
"""An https URL with no user information, query, or fragment; a trailing slash is dropped."""


class RetrySettings(ValueModel):
    """Full-jitter exponential backoff: each wait is uniform between 0 and min(cap, base * 2^attempt).

    The defaults are a tenant script's. A job's runtime uses `JOB_RETRY_SETTINGS` in `launcher.py`, which keeps
    retrying a throttled call for up to about two minutes.
    """

    attempts: int = Field(default=5, ge=1)
    base_seconds: float = Field(default=0.2, gt=0)
    cap_seconds: float = Field(default=20.0, gt=0)


class TenantDelegation(ValueModel):
    """The tenant and custom connector a job's runtime role acts for.

    The framework accepts it only from the runtime role it stored for that custom connector.
    """

    tenant_arn_prefix: TenantArnPrefix
    connector_id: ConnectorId


class CcfClientSettings(ValueModel):
    """The framework endpoint, its Region, and the session whose credentials sign every call."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    endpoint: HttpsEndpoint
    region: Region
    session: boto3.Session
    delegation: TenantDelegation | None = None
    """Set only by the job runtime; a tenant's own calls leave it out."""
    correlation_id: CorrelationId | None = None
    """Sent as `x-correlation-id` on every call; the job runtime sets it to the job ID."""
    send_deadline: SendDeadline | None = None
    """Set only by the job runtime, which starts it on `SIGTERM`; no call attempt or retry wait ends after it."""
    retry: RetrySettings = RetrySettings()
