"""Starting an ingestion through the destination role, and the disposition of each failure."""

from __future__ import annotations

from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from common.storage.bedrock.bedrock_ingestion_dao import (
    BedrockIngestionDao,
    IngestionDisposition,
    IngestionStartError,
    StartIngestionRequest,
)
from common.tenant_role_clients import TenantRoleClients

WORKFLOW_ARN = "arn:aws:states:us-east-1:444455556666:stateMachine:CcfDestinationWorkflow"
ROLE_ARN = "arn:aws:iam::111122223333:role/ccf/destinations/CcfDestinationRole-docs"
ACCESS_KEY = "ASIA" + "X" * 16
REQUEST = StartIngestionRequest(
    destination_id="dst-7c41e9a2b5d0",
    role_arn=ROLE_ARN,
    knowledge_base_id="KB12345678",
    data_source_id="DS12345678",
    client_token="ing-3f6c2d1e-0000-4000-8000-000000000000",
)


def client_error(code: str, status: int) -> ClientError:
    # Only the members the DAO reads; botocore's type wants every ResponseMetadata key.
    response: Any = {"Error": {"Code": code, "Message": "x"}, "ResponseMetadata": {"HTTPStatusCode": status}}
    return ClientError(response, "Op")


class FakeSts:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.error = error

    def assume_role(self, **arguments: Any) -> dict[str, Any]:
        self.calls.append(arguments)
        if self.error is not None:
            raise self.error
        return {"Credentials": {"AccessKeyId": ACCESS_KEY, "SecretAccessKey": "s" * 40, "SessionToken": "t"}}


class RecordingBedrock:
    def __init__(self, access_key: str, error: Exception | None) -> None:
        self.access_key = access_key
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def start_ingestion_job(self, **arguments: Any) -> None:
        self.calls.append(arguments)
        if self.error is not None:
            raise self.error


class Bedrock:
    """Patches `boto3.Session.client` to hand out one recording Bedrock client per session."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, error: Exception | None = None) -> None:
        self.created: list[RecordingBedrock] = []

        def client(session: boto3.Session, service_name: str) -> RecordingBedrock:
            assert service_name == "bedrock-agent"
            credentials = session.get_credentials()
            assert credentials is not None
            bedrock = RecordingBedrock(credentials.access_key, error)
            self.created.append(bedrock)
            return bedrock

        monkeypatch.setattr(boto3.Session, "client", client)


def start(sts: FakeSts) -> None:
    BedrockIngestionDao(TenantRoleClients(sts, "us-east-1"), WORKFLOW_ARN).start_ingestion(REQUEST)  # type: ignore[arg-type]


def test_start_ingestion_assumes_the_destination_role_and_calls_with_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bedrock = Bedrock(monkeypatch)
    sts = FakeSts()

    start(sts)

    assert sts.calls == [
        {"RoleArn": ROLE_ARN, "RoleSessionName": "ccf-ingest-dst-7c41e9a2b5d0", "ExternalId": WORKFLOW_ARN}
    ]
    [client] = bedrock.created
    assert client.access_key == ACCESS_KEY
    assert client.calls == [
        {"knowledgeBaseId": "KB12345678", "dataSourceId": "DS12345678", "clientToken": REQUEST.client_token}
    ]


@pytest.mark.parametrize(
    ("bedrock_error", "sts_error", "disposition", "code"),
    [
        (client_error("ConflictException", 409), None, IngestionDisposition.RETRY, "ConflictException"),
        (client_error("ThrottlingException", 429), None, IngestionDisposition.RETRY, "ThrottlingException"),
        (client_error("InternalServerException", 500), None, IngestionDisposition.RETRY, "InternalServerException"),
        (client_error("ValidationException", 500), None, IngestionDisposition.RETRY, "ValidationException"),
        (EndpointConnectionError(endpoint_url="https://bedrock-agent.us-east-1.amazonaws.com"), None, "RETRY", None),
        (None, client_error("Throttling", 400), IngestionDisposition.RETRY, "Throttling"),
        (client_error("AccessDeniedException", 403), None, IngestionDisposition.REJECT, "AccessDeniedException"),
        (
            client_error("ResourceNotFoundException", 404),
            None,
            IngestionDisposition.REJECT,
            "ResourceNotFoundException",
        ),
        (client_error("ValidationException", 400), None, IngestionDisposition.REJECT, "ValidationException"),
        (None, client_error("AccessDenied", 403), IngestionDisposition.REJECT, "AccessDenied"),
    ],
)
def test_each_failure_raises_its_disposition_and_aws_error_code(
    monkeypatch: pytest.MonkeyPatch,
    bedrock_error: Exception | None,
    sts_error: Exception | None,
    disposition: IngestionDisposition,
    code: str | None,
) -> None:
    Bedrock(monkeypatch, bedrock_error)

    with pytest.raises(IngestionStartError) as raised:
        start(FakeSts(sts_error))

    assert (raised.value.disposition, raised.value.aws_error_code) == (disposition, code)
