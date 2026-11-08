"""What a connector implementation gets to know about its job."""

from __future__ import annotations

import boto3
from pydantic import BaseModel, ConfigDict, InstanceOf, SecretStr


class JobContext(BaseModel):
    """One job's IDs, configuration, secret, and source-access session.

    `source_session` holds the source-access role's credentials, which the toolkit refreshes
    before they expire. It is the only AWS session the implementation gets: use it for anything
    it reads in the tenant account. Read checkpoints through the store's `get_checkpoint` or
    `collections`. There is no logger: log with the `logging` module or `print`.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    destination_id: str
    connector_id: str
    job_id: str
    connector_configuration: InstanceOf[SecretStr] | None
    """The custom connector's configuration string, exactly as registered; the framework never parses it."""
    job_configuration: InstanceOf[SecretStr] | None
    """The string given to `StartCustomConnectorJob`, if any."""
    secret: InstanceOf[SecretStr] | None
    """The value of the custom connector's `secret_arn`, read through `source_session`, or None when none is set."""
    source_session: boto3.Session
