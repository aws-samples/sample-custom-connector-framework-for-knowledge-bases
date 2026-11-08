"""Setting a knowledge base's metadata fields through the destination role, and the error each refusal becomes."""

from __future__ import annotations

from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
from ccf_api_models.models import MetadataField, MetadataFieldSearchability, MetadataFieldType

from common.errors import (
    DependencyThrottledError,
    DependencyUnavailableError,
    DestinationRoleDeniedError,
    ServiceError,
)
from common.storage.bedrock.knowledge_base_fields_dao import (
    KnowledgeBaseFieldsDao,
    KnowledgeBaseUpdatingError,
    MetadataFieldsRejectedError,
    SetMetadataFieldsRequest,
)
from common.tenant_role_clients import TenantRoleClients

WORKFLOW_ARN = "arn:aws:states:us-east-1:444455556666:stateMachine:CcfDestinationWorkflow"
ROLE_ARN = "arn:aws:iam::111122223333:role/ccf/destinations/CcfDestinationRole-docs"
KNOWLEDGE_BASE_ROLE_ARN = "arn:aws:iam::111122223333:role/ccf/destinations/CcfKnowledgeBase-dst-7c41e9a2b5d0"
OWNER = MetadataField(name="owner", type=MetadataFieldType.STRING, searchability=MetadataFieldSearchability.ENABLED)
REQUEST = SetMetadataFieldsRequest(
    destination_id="dst-7c41e9a2b5d0", role_arn=ROLE_ARN, knowledge_base_id="KB12345678", fields=(OWNER,)
)
KNOWLEDGE_BASE = {
    "name": "ccf-dst-7c41e9a2b5d0",
    "roleArn": KNOWLEDGE_BASE_ROLE_ARN,
    "knowledgeBaseConfiguration": {
        "type": "MANAGED",
        "managedKnowledgeBaseConfiguration": {"embeddingModelType": "MANAGED"},
    },
}


class FakeSts:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def assume_role(self, **arguments: Any) -> dict[str, Any]:
        self.calls.append(arguments)
        return {"Credentials": {"AccessKeyId": "ASIA" + "X" * 16, "SecretAccessKey": "s" * 40, "SessionToken": "t"}}


class RecordingBedrock:
    def __init__(self, error: Exception | None) -> None:
        self.error = error
        self.updates: list[dict[str, Any]] = []

    def get_knowledge_base(self, knowledgeBaseId: str) -> dict[str, Any]:  # noqa: N803
        return {"knowledgeBase": {**KNOWLEDGE_BASE, "knowledgeBaseId": knowledgeBaseId}}

    def update_knowledge_base(self, **arguments: Any) -> None:
        self.updates.append(arguments)
        if self.error is not None:
            raise self.error


def bedrock(monkeypatch: pytest.MonkeyPatch, error: Exception | None = None) -> RecordingBedrock:
    """Patch `boto3.Session.client` so the role's session hands out this Bedrock client."""
    recording = RecordingBedrock(error)

    def client(_session: boto3.Session, service_name: str) -> RecordingBedrock:
        assert service_name == "bedrock-agent"
        return recording

    monkeypatch.setattr(boto3.Session, "client", client)
    return recording


def dao(sts: FakeSts) -> KnowledgeBaseFieldsDao:
    return KnowledgeBaseFieldsDao(TenantRoleClients(sts, "us-east-1"), WORKFLOW_ARN)  # type: ignore[arg-type]


def client_error(code: str) -> ClientError:
    response: Any = {"Error": {"Code": code, "Message": "x"}, "ResponseMetadata": {"HTTPStatusCode": 400}}
    return ClientError(response, "UpdateKnowledgeBase")


def test_set_fields_assumes_the_destination_role_with_the_workflow_arn(monkeypatch: pytest.MonkeyPatch) -> None:
    bedrock(monkeypatch)
    sts = FakeSts()

    dao(sts).set_fields(REQUEST)

    [call] = sts.calls
    assert (call["RoleArn"], call["ExternalId"]) == (ROLE_ARN, WORKFLOW_ARN)


def test_set_fields_sends_the_knowledge_bases_own_settings_back_with_the_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recording = bedrock(monkeypatch)

    dao(FakeSts()).set_fields(REQUEST)

    assert recording.updates == [
        {
            "knowledgeBaseId": "KB12345678",
            "name": KNOWLEDGE_BASE["name"],
            "roleArn": KNOWLEDGE_BASE_ROLE_ARN,
            "knowledgeBaseConfiguration": KNOWLEDGE_BASE["knowledgeBaseConfiguration"],
            "metadataConfigurations": [{"name": "owner", "type": "STRING", "searchability": "ENABLED"}],
        }
    ]


def test_set_fields_with_none_sends_an_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    recording = bedrock(monkeypatch)

    dao(FakeSts()).set_fields(REQUEST.model_copy(update={"fields": ()}))

    assert recording.updates[0]["metadataConfigurations"] == []


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("ValidationException", MetadataFieldsRejectedError),
        ("ConflictException", KnowledgeBaseUpdatingError),
        ("AccessDeniedException", DestinationRoleDeniedError),
        ("ThrottlingException", DependencyThrottledError),
        ("InternalServerException", DependencyUnavailableError),
    ],
)
def test_set_fields_turns_each_bedrock_refusal_into_its_api_error(
    monkeypatch: pytest.MonkeyPatch, code: str, expected: type[ServiceError]
) -> None:
    bedrock(monkeypatch, client_error(code))

    with pytest.raises(expected) as raised:
        dao(FakeSts()).set_fields(REQUEST)

    assert raised.value.aws_error_code in {code, None}
