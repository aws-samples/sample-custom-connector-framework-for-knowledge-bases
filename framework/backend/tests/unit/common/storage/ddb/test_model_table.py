"""The one DynamoDB table class: item values, reads, conditional writes, transactions, and listing tokens."""

from __future__ import annotations

import base64
import json
from decimal import Decimal
from typing import Any

import pytest
from botocore.exceptions import ClientError
from ccf_api_models.contracts import TablesContract
from pydantic import BaseModel, ConfigDict

from common.aws_error_code import AwsErrorCode
from common.errors import (
    BadRequestError,
    DependencyThrottledError,
    ResourceConflictError,
    ResourceNotFoundError,
    TransactionCanceledError,
)
from common.storage.ddb.model_table import (
    ConditionCheckRequest,
    DeleteItemRequest,
    ItemKey,
    ModelTable,
    OnConditionFailure,
    PutItemRequest,
    QueryRequest,
    SortCondition,
    SortOperator,
    TransactionRequest,
    UpdateItemRequest,
)

TABLES = TablesContract.load()
TENANT = "arn:aws:ccf:us-east-1:111122223333"
OTHER_TENANT = "arn:aws:ccf:us-east-1:444455556666"


class Item(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    tenant_arn_prefix: str
    destination_id: str
    name: str
    status: str = "ACTIVE"
    created_at: float = 1.5
    description: str | None = None


class Secret(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, val_json_bytes="base64")

    connector_arn: str
    collection_id: str
    encrypted_checkpoint: bytes | None = None


def destinations(dynamodb: Any) -> ModelTable[Item]:
    return ModelTable(dynamodb.Table(TABLES.destinations.name), dynamodb.meta.client, Item, TABLES.destinations)


def item(destination_id: str = "dst-000000000001", name: str = "docs", tenant: str = TENANT) -> Item:
    return Item(tenant_arn_prefix=tenant, destination_id=destination_id, name=name)


def key(destination_id: str = "dst-000000000001") -> ItemKey:
    return ItemKey(partition=TENANT, sort=destination_id)


def token(value: dict[str, str]) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode()


def test_put_writes_numbers_as_decimals_and_leaves_out_absent_members(dynamodb: Any) -> None:
    table = destinations(dynamodb)

    table.put(PutItemRequest(item=item(), action="Destination creation"))

    stored = dynamodb.Table(TABLES.destinations.name).get_item(
        Key={"tenant_arn_prefix": TENANT, "destination_id": "dst-000000000001"}
    )["Item"]
    assert stored["created_at"] == Decimal("1.5")
    assert "description" not in stored


def test_get_reads_the_item_back_as_the_model(dynamodb: Any) -> None:
    table = destinations(dynamodb)
    table.put(PutItemRequest(item=item(), action="Destination creation"))

    assert table.get(key()) == item()


def test_a_binary_attribute_is_written_as_binary_and_read_back_as_bytes(dynamodb: Any) -> None:
    table = ModelTable(dynamodb.Table(TABLES.collections.name), dynamodb.meta.client, Secret, TABLES.collections)
    secret = Secret(connector_arn="arn:c", collection_id="col-default", encrypted_checkpoint=b"\x00\x01ciphertext")

    table.put(PutItemRequest(item=secret, action="Checkpoint write"))

    assert table.get(ItemKey(partition="arn:c", sort="col-default")) == secret


def test_get_returns_none_for_a_missing_item(dynamodb: Any) -> None:
    assert destinations(dynamodb).get(key()) is None


def test_require_raises_not_found_for_a_missing_item(dynamodb: Any) -> None:
    with pytest.raises(ResourceNotFoundError):
        destinations(dynamodb).require(key())


def test_a_put_whose_condition_fails_raises_conflict(dynamodb: Any) -> None:
    table = destinations(dynamodb)
    table.put(PutItemRequest(item=item(), action="Destination creation"))

    with pytest.raises(ResourceConflictError):
        table.put(
            PutItemRequest(item=item(), action="Destination creation", condition="attribute_not_exists(destination_id)")
        )


def test_an_update_without_names_or_values_sends_neither_map(dynamodb: Any) -> None:
    table = destinations(dynamodb)
    table.put(PutItemRequest(item=item(), action="Destination creation"))

    updated = table.update(UpdateItemRequest(key=key(), action="Destination update", expression="REMOVE description"))

    assert updated == item()


def test_an_update_whose_condition_fails_on_a_missing_item_raises_not_found(dynamodb: Any) -> None:
    with pytest.raises(ResourceNotFoundError):
        destinations(dynamodb).update(
            UpdateItemRequest(
                key=key(),
                action="Destination update",
                expression="SET #status = :status",
                condition="attribute_exists(destination_id)",
                names={"#status": "status"},
                values={":status": "DELETING"},
            )
        )


def test_an_update_whose_condition_fails_on_an_existing_item_raises_conflict(dynamodb: Any) -> None:
    table = destinations(dynamodb)
    table.put(PutItemRequest(item=item(), action="Destination creation"))

    with pytest.raises(ResourceConflictError):
        table.update(
            UpdateItemRequest(
                key=key(),
                action="Destination update",
                expression="SET #status = :deleting",
                condition="#status = :creating",
                names={"#status": "status"},
                values={":deleting": "DELETING", ":creating": "CREATING"},
            )
        )


def test_a_delete_without_a_condition_sends_no_expression_maps(dynamodb: Any) -> None:
    table = destinations(dynamodb)
    table.put(PutItemRequest(item=item(), action="Destination creation"))

    table.delete(DeleteItemRequest(key=key(), action="Destination deletion"))

    assert table.get(key()) is None


def test_a_transaction_without_maps_sends_none_inside_its_actions(dynamodb: Any) -> None:
    table = destinations(dynamodb)

    table.transact(
        TransactionRequest(
            action="Destination creation",
            writes=(
                table.put_action(PutItemRequest(item=item(), action="Destination creation")),
                table.put_action(PutItemRequest(item=item("dst-000000000002"), action="Destination creation")),
            ),
        )
    )

    assert table.get(key("dst-000000000002")) == item("dst-000000000002")


def test_a_canceled_transaction_raises_its_reasons_in_action_order_with_the_old_item(dynamodb: Any) -> None:
    table = destinations(dynamodb)
    table.put(PutItemRequest(item=item(), action="Destination creation"))

    with pytest.raises(TransactionCanceledError) as raised:
        table.transact(
            TransactionRequest(
                action="Destination update",
                writes=(
                    table.put_action(PutItemRequest(item=item("dst-000000000002"), action="Destination creation")),
                    table.condition_check(
                        ConditionCheckRequest(
                            key=key(),
                            condition="#status = :creating",
                            names={"#status": "status"},
                            values={":creating": "CREATING"},
                            on_condition_failure=OnConditionFailure.ALL_OLD,
                        )
                    ),
                ),
            )
        )

    reasons = raised.value.reasons
    assert [reason.code for reason in reasons] == [None, AwsErrorCode.CONDITIONAL_CHECK_FAILED_REASON]
    assert reasons[1].item is not None
    assert reasons[1].item["status"] == "ACTIVE"


def test_a_throttled_call_raises_the_throttling_error(dynamodb: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    table = destinations(dynamodb)

    def throttled(**_: Any) -> Any:
        raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "x"}}, "GetItem")

    monkeypatch.setattr(table._table, "get_item", throttled)

    with pytest.raises(DependencyThrottledError):
        table.get(key())


def test_a_query_pages_with_a_token_that_continues_the_listing(dynamodb: Any) -> None:
    table = destinations(dynamodb)
    for number in range(3):
        table.put(PutItemRequest(item=item(f"dst-00000000000{number}"), action="Destination creation"))

    first = table.query(QueryRequest(partition_value=TENANT, max_results=2))
    second = table.query(QueryRequest(partition_value=TENANT, max_results=2, next_token=first.next_token))

    assert [entry.destination_id for entry in first.items + second.items] == [
        f"dst-00000000000{number}" for number in range(3)
    ]
    assert second.next_token is None


def test_filter_equals_filters_the_query(dynamodb: Any) -> None:
    table = destinations(dynamodb)
    table.put(PutItemRequest(item=item(), action="Destination creation"))
    table.put(PutItemRequest(item=item("dst-000000000002").model_copy(update={"status": "DELETING"}), action="x"))

    page = table.query(QueryRequest(partition_value=TENANT, max_results=10, filter_equals={"status": "DELETING"}))

    assert [entry.destination_id for entry in page.items] == ["dst-000000000002"]


@pytest.mark.parametrize(
    "start_key",
    [
        {"tenant_arn_prefix": OTHER_TENANT, "destination_id": "dst-000000000001"},
        {"tenant_arn_prefix": TENANT, "destination_id": "dst-000000000001", "name": "docs"},
        {"tenant_arn_prefix": TENANT},
    ],
    ids=["another partition", "an extra attribute", "a missing attribute"],
)
def test_a_token_for_another_listing_is_a_bad_request(dynamodb: Any, start_key: dict[str, str]) -> None:
    with pytest.raises(BadRequestError):
        destinations(dynamodb).query(QueryRequest(partition_value=TENANT, max_results=2, next_token=token(start_key)))


def test_a_token_outside_the_sort_prefix_is_a_bad_request(dynamodb: Any) -> None:
    start_key = token({"tenant_arn_prefix": TENANT, "destination_id": "dst-ffff00000001"})

    with pytest.raises(BadRequestError):
        destinations(dynamodb).query(
            QueryRequest(
                partition_value=TENANT,
                sort_condition=SortCondition(operator=SortOperator.BEGINS_WITH, value="dst-0000"),
                max_results=2,
                next_token=start_key,
            )
        )


def test_a_token_for_another_sort_value_is_a_bad_request(dynamodb: Any) -> None:
    start_key = token({"tenant_arn_prefix": TENANT, "destination_id": "dst-000000000001", "name": "other"})

    with pytest.raises(BadRequestError):
        destinations(dynamodb).query(
            QueryRequest(
                partition_value=TENANT,
                index="ByName",
                sort_condition=SortCondition(operator=SortOperator.EQUALS, value="docs"),
                max_results=2,
                next_token=start_key,
            )
        )


@pytest.mark.parametrize("raw", ["not base64!", "bm90LWpzb24", token({"a": ["list"]})])  # type: ignore[dict-item]
def test_a_malformed_token_is_a_bad_request(dynamodb: Any, raw: str) -> None:
    with pytest.raises(BadRequestError):
        destinations(dynamodb).query(QueryRequest(partition_value=TENANT, max_results=2, next_token=raw))
