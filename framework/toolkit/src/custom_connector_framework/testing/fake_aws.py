"""In-memory STS and Secrets Manager, and boto3 sessions whose clients are fakes."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from botocore.exceptions import ClientError
from botocore.session import Session as BotocoreSession

_DEFAULT_LIFETIME = timedelta(hours=1)


class FakeSts:
    """`AssumeRole`, returning fixed fake credentials that expire `lifetime` after `now`, or raising `error`."""

    def __init__(
        self, now: datetime | None = None, lifetime: timedelta = _DEFAULT_LIFETIME, error: ClientError | None = None
    ) -> None:
        self.now = now or datetime.now(UTC)
        self.lifetime = lifetime
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def assume_role(self, **arguments: Any) -> dict[str, Any]:
        self.calls.append(arguments)
        if self.error is not None:
            raise self.error
        return {
            "Credentials": {
                "AccessKeyId": f"ASIAFAKE{len(self.calls):012d}",
                "SecretAccessKey": "fake-secret-access-key",
                "SessionToken": "fake-session-token",
                "Expiration": self.now + self.lifetime,
            }
        }


class FakeSecretsManager:
    """`GetSecretValue` over a map of secret ARNs to values; an unknown ARN raises `ResourceNotFoundException`."""

    def __init__(self, secrets: Mapping[str, str] | None = None) -> None:
        self.secrets = dict(secrets or {})
        self.calls: list[str] = []

    def get_secret_value(self, **arguments: Any) -> dict[str, Any]:
        secret_id = arguments["SecretId"]
        self.calls.append(secret_id)
        if secret_id not in self.secrets:
            raise ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "missing"}}, "GetSecretValue")
        return {"ARN": secret_id, "SecretString": self.secrets[secret_id]}


class FakeSession(boto3.Session):
    """A boto3 session whose `client()` returns the given fakes and records each service it was asked for."""

    def __init__(self, clients: Mapping[str, object], **arguments: Any) -> None:
        super().__init__(**arguments)
        self._clients = dict(clients)
        self.requested: list[str] = []

    def client(self, service_name: str, *_: Any, **__: Any) -> Any:
        self.requested.append(service_name)
        return self._clients[service_name]


class FakeSessions:
    """Builds `FakeSession`s over the same fakes and keeps each one; pass it where the toolkit builds a session."""

    def __init__(self, clients: Mapping[str, object]) -> None:
        self._clients = clients
        self.built: list[FakeSession] = []

    def __call__(self, *, botocore_session: BotocoreSession, region_name: str) -> FakeSession:
        session = FakeSession(self._clients, botocore_session=botocore_session, region_name=region_name)
        self.built.append(session)
        return session
