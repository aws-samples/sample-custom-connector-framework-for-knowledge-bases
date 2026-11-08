"""Test environment, and moto DynamoDB tables built from the tables contract the CDK reads."""

from __future__ import annotations

import os
from collections.abc import Iterator
from importlib.resources import files
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
from ccf_api_models.base import ValueModel
from ccf_api_models.catalog import OpenApiDocument
from ccf_api_models.contracts import AttributeType, KeyAttribute, TableContract, TablesContract
from moto import mock_aws
from pydantic import SecretStr

from common.resource_data_codec import (
    DecryptResourceDataRequest,
    EncryptResourceDataRequest,
    ResourceDataDecryptionError,
)
from common.settings import TableNames

os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

REGION = "us-east-1"
TABLES = TablesContract.load()
ENV_EXAMPLE = Path(__file__).resolve().parents[3] / ".env.example"
_SCHEMAS = OpenApiDocument.model_validate_json(
    files("ccf_api_models").joinpath("openapi.json").read_bytes()
).components.schemas
_ATTRIBUTE_TYPES = {AttributeType.STRING: "S", AttributeType.NUMBER: "N"}


def _key_schema(partition: KeyAttribute, sort: KeyAttribute | None) -> list[dict[str, str]]:
    keys = [{"AttributeName": partition.name, "KeyType": "HASH"}]
    return keys if sort is None else [*keys, {"AttributeName": sort.name, "KeyType": "RANGE"}]


def _create(resource: Any, contract: TableContract) -> None:
    """Create the table as the CDK does: its keys, and each index projecting its summary schema's members."""
    keys = [contract.partition_key, contract.sort_key]
    for index in contract.indexes.values():
        keys += [index.partition_key, index.sort_key]
    attributes = {key.name: _ATTRIBUTE_TYPES[key.type] for key in keys if key is not None}
    arguments: dict[str, Any] = {
        "TableName": contract.name,
        "BillingMode": "PAY_PER_REQUEST",
        "KeySchema": _key_schema(contract.partition_key, contract.sort_key),
        "AttributeDefinitions": [{"AttributeName": name, "AttributeType": kind} for name, kind in attributes.items()],
    }
    if contract.indexes:
        arguments["GlobalSecondaryIndexes"] = [
            {
                "IndexName": name,
                "KeySchema": _key_schema(index.partition_key, index.sort_key),
                "Projection": {
                    "ProjectionType": "INCLUDE",
                    "NonKeyAttributes": sorted(set(_SCHEMAS[index.projection].properties) - attributes.keys()),
                },
            }
            for name, index in contract.indexes.items()
        ]
    resource.create_table(**arguments)


@pytest.fixture
def example_environment(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Set every setting in `.env.example`, as the CDK app does in each function, and return them."""
    values = dict(
        line.split("=", 1) for line in ENV_EXAMPLE.read_text().splitlines() if line.strip() and not line.startswith("#")
    )
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return values


@pytest.fixture
def table_names() -> TableNames:
    return TableNames.model_validate({name: getattr(TABLES, name).name for name in TableNames.model_fields})


def _reject_empty_expression_maps(params: dict[str, Any], **_: Any) -> None:
    """Fail the way DynamoDB does, which moto doesn't: an empty expression name or value map is invalid,
    in a single call and inside every `TransactItems` action."""
    calls = [params, *(action for item in params.get("TransactItems", []) for action in item.values())]
    for call in calls:
        for member in ("ExpressionAttributeNames", "ExpressionAttributeValues"):
            if call.get(member) == {}:
                raise ClientError(
                    {"Error": {"Code": "ValidationException", "Message": f"{member} must not be empty"}},
                    "DynamoDB",
                )


@pytest.fixture
def dynamodb() -> Iterator[Any]:
    """A moto DynamoDB resource holding the framework's tables."""
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name=REGION)
        resource.meta.client.meta.events.register("provide-client-params.dynamodb", _reject_empty_expression_maps)
        for name in TableNames.model_fields:
            if name != "idempotency":
                _create(resource, getattr(TABLES, name))
        yield resource


class FailingDynamoDbCall(ValueModel):
    """A DynamoDB operation on one table that answers with a server error, injected below the DAO."""

    operation: str
    table_name: str


class DynamoDbFailures:
    """Makes chosen DynamoDB calls fail the way the service does, so a test fails a write at the AWS boundary."""

    def __init__(self, resource: Any) -> None:
        self._client = resource.meta.client

    def fail(self, call: FailingDynamoDbCall) -> None:
        """Answer every matching call, or transaction naming the table, with `InternalServerError`."""

        def failing(params: dict[str, Any], **_: Any) -> None:
            actions = [action for item in params.get("TransactItems", []) for action in item.values()]
            if call.table_name in {params.get("TableName"), *(action.get("TableName") for action in actions)}:
                raise ClientError({"Error": {"Code": "InternalServerError", "Message": "injected"}}, call.operation)

        self._client.meta.events.register(f"provide-client-params.dynamodb.{call.operation}", failing)


@pytest.fixture
def dynamodb_failures(dynamodb: Any) -> DynamoDbFailures:
    return DynamoDbFailures(dynamodb)


class ReversibleCodec:
    """Prefixes the value with its resource ARN, so a test sees both and a wrong ARN fails to decrypt."""

    def encrypt(self, request: EncryptResourceDataRequest) -> bytes:
        return f"{request.resource_arn}|{request.data.get_secret_value()}".encode()

    def decrypt(self, request: DecryptResourceDataRequest) -> SecretStr:
        resource_arn, value = request.ciphertext.decode().split("|", 1)
        if resource_arn != request.resource_arn:
            raise ResourceDataDecryptionError(f"Data of {request.resource_arn} could not be decrypted")
        return SecretStr(value)


@pytest.fixture
def codec() -> ReversibleCodec:
    return ReversibleCodec()
