"""Collection items, the deletion mark, the write guard, listings, and checkpoints."""

from typing import Any

import pytest
from botocore.exceptions import ClientError
from pydantic import SecretStr

from common.errors import ResourceConflictError, ResourceNotFoundError, TransactionCanceledError
from common.resource_data_codec import ResourceDataDecryptionError
from common.storage.ddb.collections_dao import (
    CollectionWriteCondition,
    PutCheckpointRecord,
    UpsertCollectionRequest,
    WriteGuardRequest,
)
from common.storage.ddb.model_table import ListItemsRequest


def listed(world: Any, collection_id: str | None = None) -> list[Any]:
    request: ListItemsRequest[Any] = ListItemsRequest(
        parent=world.connector, max_results=100, filter_value=collection_id
    )
    return list(world.collections.list(request).items)


def items(world: Any) -> list[dict[str, Any]]:
    return list(world.table(world.names.collections).scan()["Items"])


def test_upsert_stores_the_source_collection_id_only_on_first_write(world: Any) -> None:
    target = world.collection()

    world.collections.upsert(UpsertCollectionRequest(collection=target, source_collection_id="awslabs/mcp"))
    world.collections.upsert(UpsertCollectionRequest(collection=target, source_collection_id="awslabs/other"))

    assert [(item.collection_id, item.source_collection_id) for item in listed(world)] == [
        (target.collection_id, "awslabs/mcp")
    ]


def test_is_deleting_is_a_consistent_read_of_the_mark(world: Any) -> None:
    target = world.collection()
    world, spy = world.spying_on(world.names.collections)
    world.collections.upsert(UpsertCollectionRequest(collection=target, source_collection_id="awslabs/mcp"))

    before = world.collections.is_deleting(target)
    world.collections.mark_deleting(target)

    assert (before, world.collections.is_deleting(target)) == (False, True)
    assert spy.reads == [True, True]


def test_mark_deleting_a_missing_collection_returns_false_and_creates_no_item(world: Any) -> None:
    assert world.collections.mark_deleting(world.collection()) is False
    assert items(world) == []


def test_unmark_deleting_a_deleted_collection_doesnt_recreate_it(world: Any) -> None:
    world.collections.unmark_deleting(world.collection())

    assert items(world) == []


def test_delete_marked_deletes_only_a_marked_collection(world: Any) -> None:
    marked, unmarked = world.collection("col-0000000000000001"), world.collection()
    for target in (marked, unmarked):
        world.collections.upsert(UpsertCollectionRequest(collection=target, source_collection_id=target.collection_id))
    world.collections.mark_deleting(marked)

    world.collections.delete_marked(marked)
    world.collections.delete_marked(unmarked)

    assert [item["collection_id"] for item in items(world)] == [unmarked.collection_id]


@pytest.mark.parametrize("condition", list(CollectionWriteCondition))
def test_the_write_guard_fails_a_transaction_against_a_marked_collection(
    world: Any, condition: CollectionWriteCondition
) -> None:
    target = world.collection()
    world.collections.upsert(UpsertCollectionRequest(collection=target, source_collection_id="awslabs/mcp"))
    world.collections.mark_deleting(target)
    guard = world.collections.write_guard(WriteGuardRequest(collection=target, condition=condition))

    with pytest.raises(ClientError) as raised:
        world.resource.meta.client.transact_write_items(TransactItems=[guard])

    assert raised.value.response["Error"]["Code"] == "TransactionCanceledException"


def test_the_existence_guard_fails_a_transaction_against_a_missing_collection(world: Any) -> None:
    guard = world.collections.write_guard(
        WriteGuardRequest(collection=world.collection(), condition=CollectionWriteCondition.EXISTS_AND_NOT_DELETING)
    )

    with pytest.raises(ClientError):
        world.resource.meta.client.transact_write_items(TransactItems=[guard])


def test_a_listing_with_a_collection_id_returns_that_collection_or_none(world: Any) -> None:
    target, other = world.collection(), world.collection("col-0000000000000001")
    for collection in (target, other):
        world.collections.upsert(
            UpsertCollectionRequest(collection=collection, source_collection_id=collection.collection_id)
        )

    assert [item.collection_id for item in listed(world, target.collection_id)] == [target.collection_id]
    assert listed(world, "col-00000000000000ff") == []


def test_the_default_collection_has_no_source_collection_id(world: Any) -> None:
    world.collections.upsert(UpsertCollectionRequest(collection=world.collection("col-default")))

    assert listed(world)[0].source_collection_id is None


def checkpoint(world: Any, text: str, collection: Any | None = None) -> PutCheckpointRecord:
    return PutCheckpointRecord(
        collection=collection or world.collection(),
        source_collection_id="awslabs/mcp",
        checkpoint_data=SecretStr(text),
    )


def test_a_listed_checkpoint_comes_back_decrypted(world: Any) -> None:
    world.available_connector()
    world.collections.put_checkpoint(checkpoint(world, "cursor=7"))

    [collection] = listed(world)

    assert collection.checkpoint_data is not None
    assert collection.checkpoint_data.get_secret_value() == "cursor=7"


def test_a_first_checkpoint_put_creates_the_item_with_its_source_collection_id(world: Any) -> None:
    world.available_connector()

    world.collections.put_checkpoint(checkpoint(world, "cursor=1"))

    assert [(item.collection_id, item.source_collection_id) for item in listed(world)] == [
        (world.collection().collection_id, "awslabs/mcp")
    ]


def test_a_second_checkpoint_put_replaces_the_text_and_keeps_both_creation_times(world: Any) -> None:
    world.available_connector()
    world.collections.put_checkpoint(checkpoint(world, "not JSON, cursor=1"))
    first = items(world)[0]

    world.collections.put_checkpoint(checkpoint(world, "not JSON, cursor=2"))

    second = items(world)[0]
    stored = world.collections.get_checkpoint(world.collection()).checkpoint
    assert stored is not None and stored.checkpoint_data.get_secret_value() == "not JSON, cursor=2"
    assert (second["created_at"], second["checkpoint_created_at"]) == (
        first["created_at"],
        first["checkpoint_created_at"],
    )


def test_a_deleted_checkpoint_reads_back_as_a_response_with_no_checkpoint(world: Any) -> None:
    world.available_connector()
    world.collections.put_checkpoint(checkpoint(world, "cursor=1"))

    world.collections.delete_checkpoint(world.collection())

    assert world.collections.get_checkpoint(world.collection()).checkpoint is None


def test_a_checkpoint_put_for_a_custom_connector_that_doesnt_exist_is_not_found_and_writes_nothing(
    world: Any,
) -> None:
    with pytest.raises(ResourceNotFoundError):
        world.collections.put_checkpoint(checkpoint(world, "cursor=1"))

    assert items(world) == []


def test_a_checkpoint_delete_while_a_job_runs_conflicts_and_keeps_the_checkpoint(world: Any) -> None:
    world.started_job()
    world.collections.put_checkpoint(checkpoint(world, "cursor=1"))

    with pytest.raises(ResourceConflictError) as raised:
        world.collections.delete_checkpoint(world.collection())

    assert not isinstance(raised.value, TransactionCanceledError)
    assert world.collections.get_checkpoint(world.collection()).checkpoint is not None


def test_a_checkpoint_delete_for_a_custom_connector_that_doesnt_exist_is_not_found(world: Any) -> None:
    with pytest.raises(ResourceNotFoundError):
        world.collections.delete_checkpoint(world.collection())


def test_a_checkpoint_delete_without_a_collection_item_returns_and_creates_no_item(world: Any) -> None:
    world.available_connector()

    world.collections.delete_checkpoint(world.collection())

    assert items(world) == []


def test_a_checkpoint_delete_on_a_deleting_custom_connector_removes_the_checkpoint(world: Any) -> None:
    world.available_connector()
    world.collections.put_checkpoint(checkpoint(world, "cursor=1"))
    world.connectors.begin_deletion(world.connector)

    world.collections.delete_checkpoint(world.collection())

    assert world.collections.get_checkpoint(world.collection()).checkpoint is None


def test_a_checkpoint_get_with_nothing_stored_returns_a_response_whose_checkpoint_is_unset(world: Any) -> None:
    response = world.collections.get_checkpoint(world.collection())

    assert response.checkpoint is None
    assert "checkpoint" not in response.model_fields_set


def test_a_checkpoint_records_repr_doesnt_show_the_checkpoint_text(world: Any) -> None:
    assert "cursor=secret-text" not in repr(checkpoint(world, "cursor=secret-text"))


def test_put_checkpoint_given_a_checkpoint_when_stored_then_the_item_holds_only_its_ciphertext(world: Any) -> None:
    world.available_connector()
    target = world.collection()

    world.collections.put_checkpoint(checkpoint(world, "cursor=1"))

    item = world.table(world.names.collections).get_item(
        Key={"connector_arn": world.connector.arn, "collection_id": target.collection_id}
    )["Item"]
    assert "checkpoint_data" not in item
    assert bytes(item["encrypted_checkpoint"]) == f"{target.arn}|cursor=1".encode()


def test_get_checkpoint_given_ciphertext_of_another_collection_when_read_then_fails_to_decrypt(world: Any) -> None:
    world.available_connector()
    source, target = world.collection("col-0000000000000001"), world.collection()
    for collection in (source, target):
        world.collections.upsert(
            UpsertCollectionRequest(collection=collection, source_collection_id=f"awslabs/{collection.collection_id}")
        )
    world.collections.put_checkpoint(checkpoint(world, "cursor=1", source))
    table = world.table(world.names.collections)
    copied = table.get_item(Key={"connector_arn": world.connector.arn, "collection_id": source.collection_id})["Item"]
    table.update_item(
        Key={"connector_arn": world.connector.arn, "collection_id": target.collection_id},
        UpdateExpression="SET encrypted_checkpoint = :c, checkpoint_created_at = :t, checkpoint_updated_at = :t",
        ExpressionAttributeValues={":c": copied["encrypted_checkpoint"], ":t": 1},
    )

    with pytest.raises(ResourceDataDecryptionError):
        world.collections.get_checkpoint(target)
