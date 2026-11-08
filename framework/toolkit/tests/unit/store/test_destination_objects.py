"""The toolkit's one S3 DAO, against the testing `FakeS3`."""

from __future__ import annotations

import io

from custom_connector_framework.store.destination_objects import DestinationObjects, PutObjectRequest
from custom_connector_framework.testing import FakeS3

BUCKET = "ccf-dst-7c41e9a2b5d0-111122223333-us-east-1"
PREFIX = "connector_id=cc-e020d65d76c0/collection_id=col-default/document_id=doc-8d41e07a9c3f2b6e/"


def bucket_with(keys: list[str]) -> tuple[FakeS3, DestinationObjects]:
    s3 = FakeS3()
    objects = DestinationObjects(s3, BUCKET)  # type: ignore[arg-type]
    for key in keys:
        objects.put(PutObjectRequest(key=key, body=io.BytesIO(b"x")))
    s3.calls.clear()
    return s3, objects


def test_list_keys_pages_through_the_paginator_and_reads_each_last_modified_time() -> None:
    s3, objects = bucket_with([f"{PREFIX}README.md", f"{PREFIX}README.md.metadata.json", "connector_id=other/x"])

    listed = objects.list_keys(PREFIX)

    assert [entry.key for entry in listed] == [f"{PREFIX}README.md", f"{PREFIX}README.md.metadata.json"]
    assert listed[0].last_modified < listed[1].last_modified
    assert s3.calls[0] == ("GetPaginator", "list_objects_v2")


def test_the_content_hash_is_read_from_the_objects_metadata() -> None:
    _, objects = bucket_with([])
    objects.put(PutObjectRequest(key=f"{PREFIX}README.md", body=io.BytesIO(b"# Hi"), content_hash="sha256:9f2c"))

    assert objects.read_content_hash(f"{PREFIX}README.md") == "sha256:9f2c"


def test_an_object_put_without_a_hash_has_none() -> None:
    _, objects = bucket_with([f"{PREFIX}README.md.metadata.json"])

    assert objects.read_content_hash(f"{PREFIX}README.md.metadata.json") is None


def test_a_refused_key_comes_back_as_a_count_and_code_without_the_key() -> None:
    s3, objects = bucket_with([f"{PREFIX}README.md", f"{PREFIX}README.md.metadata.json"])
    s3.refused_deletes = {f"{PREFIX}README.md"}

    failure = objects.delete([f"{PREFIX}README.md", f"{PREFIX}README.md.metadata.json"])

    assert (failure.count, failure.codes) == (1, ("AccessDenied",))
    assert "README" not in failure.model_dump_json()
    assert list(s3.files()) == [f"{PREFIX}README.md"]


def test_2500_keys_take_three_delete_objects_calls() -> None:
    keys = [f"{PREFIX}page-{number}.md" for number in range(2_500)]
    s3, objects = bucket_with(keys)
    batches: list[int] = []
    delete_objects = s3.delete_objects

    def recording(**arguments: object) -> dict[str, object]:
        batches.append(len(arguments["Delete"]["Objects"]))  # type: ignore[index]
        return delete_objects(**arguments)

    s3.delete_objects = recording  # type: ignore[method-assign]

    assert objects.delete(keys).count == 0
    assert batches == [1_000, 1_000, 500]
