"""The document store: writes, skips, queues, checkpoints, collections, access control, and its report."""

from __future__ import annotations

import io
import json
import logging
from typing import Any

import boto3
import pytest
from ccf_api_models.base import ValueModel
from ccf_api_models.identifiers import CollectionIdentity, SourceIdKind
from ccf_api_models.models import (
    BatchDeleteCustomConnectorDocumentsRequest,
    BatchPutCustomConnectorDocumentsRequest,
    Collection,
    Destination,
    DestinationResources,
    DestinationStatus,
    DestinationType,
    ErrorCode,
    ListCustomConnectorCollectionsRequest,
    PutCustomConnectorCheckpointRequest,
)
from ccf_api_models.operations import Operation
from pydantic import BaseModel, SecretStr, ValidationError

from custom_connector_framework.client import ApiFailure, CcfApiError
from custom_connector_framework.client.pages import Pages
from custom_connector_framework.store import (
    AccessControl,
    AccessControlEntry,
    AccessEffect,
    DeleteCollectionRequest,
    DeleteDocumentRequest,
    DiffRequest,
    DocumentAttribute,
    DocumentStore,
    GetCheckpointRequest,
    ListDocumentsRequest,
    PrincipalType,
    PutDocumentRequest,
    ReportCounts,
    SaveCheckpointRequest,
    SkipReason,
    SourceDocument,
    StoredCollection,
    StoreReport,
    StoreTarget,
    StringAttribute,
    UnsupportedAccessControlError,
)
from custom_connector_framework.store.content_reader import ContentReader, ReadContentRequest, SpooledContent
from custom_connector_framework.store.destination_formats import DestinationFormats
from custom_connector_framework.store.destination_objects import DestinationObjects, FileDeleteError
from custom_connector_framework.store.file_writer import FileWriter
from custom_connector_framework.store.media_types import MediaTypes
from custom_connector_framework.store.record_queues import RecordQueues
from custom_connector_framework.store.report import StoreCounters
from custom_connector_framework.testing import FakeCcfApi, FakeS3

BUCKET = "ccf-dst-7c41e9a2b5d0-111122223333-us-east-1"
TARGET = StoreTarget(
    tenant_arn_prefix="arn:aws:ccf:us-east-1:111122223333",
    destination_id="dst-7c41e9a2b5d0",
    connector_id="cc-e020d65d76c0",
)
REPOSITORY = "630587223"
USER = AccessControlEntry(principal_id="dev@example.com", principal_type=PrincipalType.USER, access=AccessEffect.ALLOW)
GROUP = AccessControlEntry(principal_id="maintainers", principal_type=PrincipalType.GROUP, access=AccessEffect.DENY)
BATCH_PUT = Operation.BATCH_PUT_CUSTOM_CONNECTOR_DOCUMENTS.value
BATCH_DELETE = Operation.BATCH_DELETE_CUSTOM_CONNECTOR_DOCUMENTS.value
LIST_DOCUMENTS = Operation.LIST_CUSTOM_CONNECTOR_DOCUMENTS.value
LIST_COLLECTIONS = Operation.LIST_CUSTOM_CONNECTOR_COLLECTIONS.value
DELETE_COLLECTIONS = Operation.BATCH_DELETE_CUSTOM_CONNECTOR_COLLECTIONS.value
PUT_CHECKPOINT = Operation.PUT_CUSTOM_CONNECTOR_CHECKPOINT.value


class Doc(ValueModel):
    """One document a test puts, by its source IDs."""

    document_id: str = "README.md"
    content: bytes = b"# Hello\n"
    file_name: str | None = None
    collection_id: str | None = None
    source_version: str | None = None
    media_type: str | None = None
    access_control: AccessControl | None = None
    attributes: tuple[DocumentAttribute, ...] = ()

    def request(self) -> PutDocumentRequest:
        return PutDocumentRequest(
            document_id=self.document_id,
            file_name=self.file_name or self.document_id.rsplit("/", 1)[-1],
            content=io.BytesIO(self.content),
            collection_id=self.collection_id,
            source_version=self.source_version,
            media_type=self.media_type,
            access_control=self.access_control,
            attributes=self.attributes,
        )


class StoreSetup(ValueModel):
    destination_type: DestinationType = DestinationType.BEDROCK
    access_control_enabled: bool = False
    page_size: int = 50


class ClosedContentSpy(ContentReader):
    """A content reader that keeps every spooled file it returned."""

    def __init__(self) -> None:
        self.spooled: list[SpooledContent] = []

    def read(self, request: ReadContentRequest) -> SpooledContent:
        content = super().read(request)
        self.spooled.append(content)
        return content


class World:
    """A store on `FakeCcfApi`, whose bucket is the one the fake framework deletes files from."""

    def __init__(self, setup: StoreSetup) -> None:
        self.api = FakeCcfApi(page_size=setup.page_size)
        self.s3: FakeS3 = self.api.s3
        self.reader = ClosedContentSpy()
        counters = StoreCounters()
        self.store = DocumentStore(
            target=TARGET,
            api=self.api,
            pages=Pages(self.api),
            destination_format=DestinationFormats().select(destination(setup)),
            media_types=MediaTypes(),
            reader=self.reader,
            files=FileWriter(DestinationObjects(self.s3, BUCKET)),  # type: ignore[arg-type]
            queues=RecordQueues(self.api, TARGET, counters),
            counters=counters,
        )

    def put(self, doc: Doc) -> None:
        self.store.put(doc.request())

    def forget_calls(self) -> None:
        self.api.calls.clear()
        self.api.requests.clear()
        self.s3.calls.clear()

    def sent(self, operation: str) -> list[Any]:
        return [request for name, request in zip(self.api.calls, self.api.requests, strict=True) if name == operation]

    def metadata(self, key: str) -> dict[str, Any]:
        return json.loads(self.s3.objects[key].body)  # type: ignore[no-any-return]

    def s3_calls(self, operation: str) -> list[str]:
        return [key for name, key in self.s3.calls if name == operation]


def destination(setup: StoreSetup) -> Destination:
    return Destination(
        destination_id=TARGET.destination_id,
        arn=f"{TARGET.tenant_arn_prefix}:destination/{TARGET.destination_id}",
        name="docs",
        type=setup.destination_type,
        access_control_enabled=setup.access_control_enabled,
        status=DestinationStatus.ACTIVE,
        role_arn="arn:aws:iam::111122223333:role/ccf/destinations/CcfDestinationRole-docs",
        destination_resources=DestinationResources(bucket_arn=f"arn:aws:s3:::{BUCKET}", bucket_name=BUCKET),
        created_at=1.0,
        updated_at=1.0,
    )


def prefix(doc: Doc) -> str:
    return (
        f"connector_id={TARGET.connector_id}/collection_id={CollectionIdentity.of(doc.collection_id).collection_id}"
        f"/document_id={SourceIdKind.DOCUMENT.of(doc.document_id)}/"
    )


def _collection(report: StoreReport, collection_id: str | None) -> ReportCounts:
    [collection] = [collection for collection in report.collections if collection.collection_id == collection_id]
    return collection.counts


@pytest.fixture
def world() -> World:
    return World(StoreSetup())


# Writing files


def test_every_key_the_store_touches_is_under_the_documents_labeled_prefix(world: World) -> None:
    world.put(Doc(collection_id=REPOSITORY))
    world.put(Doc(collection_id=REPOSITORY, file_name="readme.md"))

    assert world.s3.calls
    assert all(
        key.startswith(prefix(Doc(collection_id=REPOSITORY))) for name, key in world.s3.calls if name != "GetPaginator"
    )


def test_a_new_document_lists_its_prefix_writes_the_metadata_file_first_and_never_calls_head_object(
    world: World,
) -> None:
    world.put(Doc())

    document_prefix = prefix(Doc())
    assert world.s3.calls == [
        ("GetPaginator", "list_objects_v2"),
        ("ListObjectsV2", document_prefix),
        ("PutObject", f"{document_prefix}README.md.metadata.json"),
        ("PutObject", f"{document_prefix}README.md"),
    ]
    assert world.s3.objects[f"{document_prefix}README.md"].body == b"# Hello\n"


def test_an_unchanged_document_writes_nothing_and_still_queues_its_record(world: World) -> None:
    world.put(Doc(source_version="a1"))
    world.forget_calls()

    world.put(Doc(source_version="a2"))
    world.store.flush()

    assert world.s3_calls("PutObject") == []
    assert world.s3_calls("HeadObject") == [f"{prefix(Doc())}README.md"]
    [stored] = world.api.documents.values()
    assert stored.source_version == "a2"
    assert world.store.get_report().total.unchanged == 1


def test_a_changed_document_overwrites_its_pair_in_place_and_deletes_nothing(world: World) -> None:
    world.put(Doc())
    world.forget_calls()

    world.put(Doc(content=b"# Hello again\n"))

    document_prefix = prefix(Doc())
    assert world.s3_calls("PutObject") == [f"{document_prefix}README.md.metadata.json", f"{document_prefix}README.md"]
    assert world.s3_calls("DeleteObjects") == []
    assert world.s3.objects[f"{document_prefix}README.md"].body == b"# Hello again\n"


def test_a_renamed_document_writes_the_new_pair_and_then_deletes_the_old_pair(world: World) -> None:
    world.put(Doc(file_name="README.md"))
    world.forget_calls()

    world.put(Doc(file_name="README.rst.md"))

    document_prefix = prefix(Doc())
    assert world.s3.calls[-3:] == [
        ("PutObject", f"{document_prefix}README.rst.md"),
        ("DeleteObjects", f"{document_prefix}README.md"),
        ("DeleteObjects", f"{document_prefix}README.md.metadata.json"),
    ]
    assert sorted(world.s3.objects) == [
        f"{document_prefix}README.rst.md",
        f"{document_prefix}README.rst.md.metadata.json",
    ]


def test_a_renamed_document_whose_old_file_s3_refuses_to_delete_raises_with_the_count_and_codes(world: World) -> None:
    world.put(Doc(file_name="README.md"))
    world.s3.refused_deletes = set(world.s3.objects)

    with pytest.raises(FileDeleteError) as raised:
        world.put(Doc(file_name="README.rst.md"))

    assert (raised.value.failure.count, raised.value.failure.codes) == (2, ("AccessDenied",))
    assert "README.md" not in str(raised.value)


@pytest.mark.parametrize(
    ("doc", "reason"),
    [
        (Doc(document_id="notes", content=b"  \n\t"), SkipReason.EMPTY_CONTENT),
        (
            Doc(document_id="notes", content=b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR", file_name="logo.png"),
            SkipReason.UNSUPPORTED_CONTENT,
        ),
        (
            Doc(document_id="notes", content=b"slides", media_type="application/vnd.ms-powerpoint"),
            SkipReason.UNSUPPORTED_CONTENT,
        ),
        (
            Doc(document_id="notes", content=b"caf\xc3\xa9 " * 3000 + b"\xff", file_name="notes.txt"),
            SkipReason.UNSUPPORTED_CONTENT,
        ),
    ],
)
def test_documents_bedrock_cant_ingest_are_skipped_with_their_reason(
    world: World, caplog: pytest.LogCaptureFixture, doc: Doc, reason: SkipReason
) -> None:
    with caplog.at_level(logging.INFO):
        world.put(doc)

    assert world.s3_calls("PutObject") == []
    assert world.store.get_report().total.skipped.model_dump()[reason.value] == 1
    [record] = [record for record in caplog.records if record.message == "document_skipped"]
    assert (record.reason, record.document_id) == (reason.value, SourceIdKind.DOCUMENT.of("notes"))  # type: ignore[attr-defined]


def test_the_store_builds_no_client_or_session(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*arguments: Any, **keywords: Any) -> Any:
        raise AssertionError("the store must not build AWS clients or sessions")

    monkeypatch.setattr(boto3, "client", refuse)
    monkeypatch.setattr(boto3, "Session", refuse)
    built = World(StoreSetup())

    built.put(Doc())
    built.store.flush()

    assert built.s3.calls


@pytest.mark.parametrize("content", [b"# Hello\n", b" \n"])
def test_the_spooled_content_is_closed_after_a_write_or_a_skip(world: World, content: bytes) -> None:
    world.put(Doc(content=content))

    [spooled] = world.reader.spooled
    assert spooled.file.closed


# Validation before any file is written


def test_a_put_with_a_source_version_over_the_apis_limit_raises_before_any_put_object(world: World) -> None:
    with pytest.raises(ValidationError, match="source_version"):
        world.put(Doc(source_version="v" * 1025))

    assert world.s3_calls("PutObject") == []
    assert world.api.calls == []


@pytest.mark.parametrize("destination_type", list(DestinationType))
@pytest.mark.parametrize("name", ["document_arn", "title", "source_uri", "_source_uri"])
def test_an_attribute_with_a_name_the_store_writes_is_rejected_before_any_s3_call(
    destination_type: DestinationType, name: str
) -> None:
    world = World(StoreSetup(destination_type=destination_type))

    with pytest.raises(ValidationError, match="reserved"):
        world.put(
            Doc(
                attributes=(
                    DocumentAttribute(
                        name=name, value=StringAttribute(value="arn:aws:ccf:us-east-1:444455556666:other")
                    ),
                )
            )
        )

    assert world.s3.calls == []


def test_put_given_two_attributes_with_one_name_when_validated_then_is_rejected_before_any_s3_call(
    world: World,
) -> None:
    with pytest.raises(ValidationError, match="more than once"):
        world.put(
            Doc(
                attributes=(
                    DocumentAttribute(name="language", value=StringAttribute(value="Python")),
                    DocumentAttribute(name="language", value=StringAttribute(value="Rust")),
                )
            )
        )

    assert world.s3.calls == []


@pytest.mark.parametrize(
    "members",
    [
        {"document_id": ""},
        {"document_id": "x" * 1025},
        {"collection_id": ""},
        {"collection_id": "c" * 1025},
        {"file_name": ""},
        {"file_name": "../escape.md"},
    ],
)
def test_a_put_with_an_invalid_id_or_file_name_raises_and_writes_nothing(world: World, members: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        world.store.put(
            PutDocumentRequest(
                **{"document_id": "README.md", "file_name": "README.md", **members}, content=io.BytesIO(b"# Hi\n")
            )
        )

    assert world.s3.calls == []


@pytest.mark.parametrize("members", [{"document_id": ""}, {"collection_id": "c" * 1025}, {"unknown": "x"}])
def test_a_delete_with_an_invalid_member_raises(members: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        DeleteDocumentRequest(**{"document_id": "README.md", **members})


# Queues and checkpoints


def test_99_records_in_one_collection_are_sent_as_one_batch(world: World) -> None:
    for number in range(99):
        world.put(Doc(document_id=f"{REPOSITORY}/file-{number}.md", collection_id=REPOSITORY))

    [sent] = world.sent(BATCH_PUT)
    assert isinstance(sent, BatchPutCustomConnectorDocumentsRequest)
    assert len(sent.documents) == 99
    assert world.store.get_report().total.pending == 0


def test_1000_records_across_collections_send_the_fullest_collection(world: World) -> None:
    for collection in range(11):
        for number in range(91 if collection < 10 else 89):
            world.store.delete(
                DeleteDocumentRequest(document_id=f"{collection}/{number}", collection_id=f"repo-{collection}")
            )
    assert world.api.calls == []

    world.store.delete(DeleteDocumentRequest(document_id="0/extra", collection_id="repo-0"))

    [sent] = world.sent(BATCH_DELETE)
    assert isinstance(sent, BatchDeleteCustomConnectorDocumentsRequest)
    assert sent.collection_id == CollectionIdentity.of("repo-0").collection_id and len(sent.document_ids) == 92


def test_a_collections_puts_go_before_its_deletes_and_its_checkpoint_last(world: World) -> None:
    world.store.delete(DeleteDocumentRequest(document_id=f"{REPOSITORY}/old.md", collection_id=REPOSITORY))
    world.put(Doc(document_id=f"{REPOSITORY}/new.md", collection_id=REPOSITORY))

    world.store.save_checkpoint(SaveCheckpointRequest(checkpoint='{"pushed_at": "x"}', collection_id=REPOSITORY))

    assert world.api.calls == [BATCH_PUT, BATCH_DELETE, PUT_CHECKPOINT]
    [put] = world.sent(BATCH_PUT)
    [checkpoint] = world.sent(PUT_CHECKPOINT)
    assert isinstance(put, BatchPutCustomConnectorDocumentsRequest)
    assert isinstance(checkpoint, PutCustomConnectorCheckpointRequest)
    assert put.source_collection_id == REPOSITORY
    assert put.collection_id == CollectionIdentity.of(REPOSITORY).collection_id
    assert checkpoint.source_collection_id == REPOSITORY


class Cursor(BaseModel):
    page: int


@pytest.mark.parametrize("checkpoint", ["x", "€" * 5_462], ids=["one-character", "over-16384-utf8-bytes"])
def test_a_checkpoint_outside_the_limits_fails_before_anything_is_sent(world: World, checkpoint: str) -> None:
    world.put(Doc())

    with pytest.raises(ValueError):
        world.store.save_checkpoint(SaveCheckpointRequest(checkpoint=checkpoint))

    assert world.api.calls == []


def test_a_collection_with_no_checkpoint_reads_as_none(world: World) -> None:
    assert world.store.get_checkpoint(GetCheckpointRequest(checkpoint_type=Cursor)) is None


def test_a_saved_checkpoint_reads_back_as_the_requested_model(world: World) -> None:
    world.store.save_checkpoint(SaveCheckpointRequest(checkpoint='{"page": 42}', collection_id=REPOSITORY))

    assert world.store.get_checkpoint(GetCheckpointRequest(checkpoint_type=Cursor, collection_id=REPOSITORY)) == Cursor(
        page=42
    )


def test_checkpoint_text_the_model_rejects_reads_as_none_with_one_warning_and_no_text(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    world.store.save_checkpoint(SaveCheckpointRequest(checkpoint="cursor=private-text"))

    with caplog.at_level(logging.WARNING):
        assert world.store.get_checkpoint(GetCheckpointRequest(checkpoint_type=Cursor)) is None

    [warning] = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert (warning.getMessage(), warning.collection_id) == ("checkpoint_unreadable", "col-default")  # type: ignore[attr-defined]
    assert all("private-text" not in str(vars(record)) for record in caplog.records)


def test_a_checkpoint_saved_in_the_default_collection_reads_back(world: World) -> None:
    world.store.save_checkpoint(SaveCheckpointRequest(checkpoint='{"page": 3}'))

    [sent] = world.sent(PUT_CHECKPOINT)
    assert "source_collection_id" not in sent.model_dump_for_wire()
    assert world.store.get_checkpoint(GetCheckpointRequest(checkpoint_type=Cursor)) == Cursor(page=3)


def test_a_throttled_checkpoint_read_raises_instead_of_reading_as_none(world: World) -> None:
    world.api.failures[Operation.GET_CUSTOM_CONNECTOR_CHECKPOINT] = CcfApiError(
        ApiFailure(status_code=429, code=ErrorCode.THROTTLED, message="slow", request_id="r")
    )

    with pytest.raises(CcfApiError):
        world.store.get_checkpoint(GetCheckpointRequest(checkpoint_type=Cursor))


def test_a_listed_collection_the_job_leaves_alone_counts_as_unchanged(world: World) -> None:
    collection = CollectionIdentity.of(REPOSITORY)
    world.api.collections[collection.collection_id] = Collection(
        collection_id=collection.collection_id,
        source_collection_id=REPOSITORY,
        checkpoint_data=SecretStr('{"page": 1}'),
        created_at=1.0,
        updated_at=1.0,
    )

    list(world.store.collections())

    assert world.store.get_report().collection_counts.unchanged == 1


def test_flush_after_failure_sends_every_put_before_any_delete(world: World) -> None:
    world.store.delete(DeleteDocumentRequest(document_id="a/old", collection_id="a"))
    world.put(Doc(document_id="b/new.md", collection_id="b"))

    world.store.flush_after_failure()

    assert world.api.calls == [BATCH_PUT, BATCH_DELETE]


# Reading back


def test_collections_yields_source_collection_ids_and_none_for_the_default_collection() -> None:
    world = World(StoreSetup(page_size=2))
    world.put(Doc())
    for repository in ("1", "2", "3"):
        world.put(Doc(document_id=f"{repository}/README.md", collection_id=repository))
    world.store.flush()

    assert sorted(world.store.collections(), key=lambda c: str(c.collection_id)) == [
        StoredCollection(collection_id="1", checkpoint=None),
        StoredCollection(collection_id="2", checkpoint=None),
        StoredCollection(collection_id="3", checkpoint=None),
        StoredCollection(collection_id=None, checkpoint=None),
    ]


def _three_stored_documents(world: World) -> None:
    for name in ("a.md", "b.md", "c.md"):
        world.put(Doc(document_id=name, source_version="1"))
    world.store.flush()
    world.forget_calls()


def test_documents_pages_through_the_listing_and_only_reads() -> None:
    world = World(StoreSetup(page_size=2))
    _three_stored_documents(world)

    listed = list(world.store.documents(ListDocumentsRequest()))

    assert sorted(document.document_id for document in listed) == ["a.md", "b.md", "c.md"]
    assert world.api.calls == [LIST_DOCUMENTS, LIST_DOCUMENTS]


def test_diff_names_new_changed_and_missing_documents_and_only_reads(world: World) -> None:
    _three_stored_documents(world)

    diff = world.store.diff(
        DiffRequest(
            current=(
                SourceDocument(document_id="a.md", source_version="1"),
                SourceDocument(document_id="b.md", source_version="2"),
                SourceDocument(document_id="d.md", source_version="1"),
            )
        )
    )

    assert (diff.new, diff.changed, diff.missing) == (("d.md",), ("b.md",), ("c.md",))
    assert set(world.api.calls) == {LIST_DOCUMENTS}


def test_delete_collection_deletes_its_documents_then_the_collection(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    world.put(Doc(document_id=f"{REPOSITORY}/README.md", collection_id=REPOSITORY))
    world.store.flush()
    world.forget_calls()

    with caplog.at_level(logging.INFO):
        world.store.delete_collection(DeleteCollectionRequest(collection_id=REPOSITORY))

    assert world.api.calls[-2:] == [BATCH_DELETE, DELETE_COLLECTIONS]
    assert world.api.collections == {}
    assert _collection(world.store.get_report(), REPOSITORY).deleted == 1
    assert any(record.message == "document_delete_queued" for record in caplog.records)


def test_delete_collection_looks_up_only_its_own_collection(world: World) -> None:
    world.put(Doc(document_id=f"{REPOSITORY}/README.md", collection_id=REPOSITORY))
    world.store.flush()

    world.store.delete_collection(DeleteCollectionRequest(collection_id=REPOSITORY))

    lookups = world.sent(LIST_COLLECTIONS)
    assert lookups
    assert all(isinstance(lookup, ListCustomConnectorCollectionsRequest) for lookup in lookups)
    assert {lookup.collection_id for lookup in lookups} == {CollectionIdentity.of(REPOSITORY).collection_id}


def test_delete_collection_deletes_100_documents_in_batches_of_99_and_1(world: World) -> None:
    for number in range(100):
        world.put(Doc(document_id=f"{REPOSITORY}/file-{number}.md", collection_id=REPOSITORY))
    world.store.flush()
    world.forget_calls()

    world.store.delete_collection(DeleteCollectionRequest(collection_id=REPOSITORY))

    assert [len(request.document_ids) for request in world.sent(BATCH_DELETE)] == [99, 1]
    assert world.api.calls[-1] == DELETE_COLLECTIONS


def test_delete_collection_of_an_unknown_id_does_nothing(world: World) -> None:
    world.store.delete_collection(DeleteCollectionRequest(collection_id="never-stored"))

    assert world.api.calls == [LIST_COLLECTIONS]


def test_delete_collection_turns_its_queued_puts_into_deletes(world: World) -> None:
    world.put(Doc(document_id=f"{REPOSITORY}/README.md", collection_id=REPOSITORY))
    world.store.flush()
    world.put(Doc(document_id=f"{REPOSITORY}/NEW.md", collection_id=REPOSITORY))
    world.forget_calls()

    world.store.delete_collection(DeleteCollectionRequest(collection_id=REPOSITORY))

    [deleted] = world.sent(BATCH_DELETE)
    assert set(deleted.document_ids) == {
        SourceIdKind.DOCUMENT.of(f"{REPOSITORY}/README.md"),
        SourceIdKind.DOCUMENT.of(f"{REPOSITORY}/NEW.md"),
    }
    assert BATCH_PUT not in world.api.calls


def test_delete_collection_of_a_collection_only_queued_deletes_its_files_records_and_collection(world: World) -> None:
    world.put(Doc(document_id=f"{REPOSITORY}/NEW.md", collection_id=REPOSITORY))
    world.forget_calls()

    world.store.delete_collection(DeleteCollectionRequest(collection_id=REPOSITORY))

    assert world.api.calls[-2:] == [BATCH_DELETE, DELETE_COLLECTIONS]
    assert BATCH_PUT not in world.api.calls
    assert world.s3.objects == {}


def test_get_report_changes_nothing(world: World) -> None:
    world.put(Doc())
    world.forget_calls()

    first, second = world.store.get_report(), world.store.get_report()

    assert first == second
    assert first.total.written == 1 and first.total.pending == 1
    assert world.api.calls == [] and world.s3.calls == []


def test_a_queued_delete_counts_as_pending_in_its_collection_and_the_total(world: World) -> None:
    world.store.delete(DeleteDocumentRequest(document_id="OLD.md", collection_id=REPOSITORY))

    report = world.store.get_report()

    assert _collection(report, REPOSITORY).pending == 1
    assert report.total.pending == 1


def test_get_report_given_puts_in_two_collections_when_read_then_lists_each_by_source_collection_id(
    world: World,
) -> None:
    world.put(Doc(document_id="a.md", collection_id=REPOSITORY))
    world.put(Doc(document_id="b.md"))

    report = world.store.get_report()

    assert [collection.collection_id for collection in report.collections] == [REPOSITORY, None]
    assert [collection.counts.written for collection in report.collections] == [1, 1]


# Skipped documents


def test_a_skipped_put_of_a_stored_document_queues_its_delete(world: World) -> None:
    world.put(Doc(document_id="notes.md", content=b"# Notes\n"))
    world.store.flush()
    world.forget_calls()

    world.put(Doc(document_id="notes.md", content=b"   \n"))
    world.store.flush()

    [deleted] = world.sent(BATCH_DELETE)
    assert deleted.document_ids == [SourceIdKind.DOCUMENT.of("notes.md")]
    assert world.api.documents == {}


def test_a_new_document_that_is_skipped_queues_nothing(world: World) -> None:
    world.put(Doc(document_id="empty.md", content=b"  \n"))
    world.store.flush()

    assert [name for name in world.api.calls if name.startswith("Batch")] == []


def test_a_document_put_earlier_in_the_job_and_then_skipped_is_deleted(world: World) -> None:
    world.put(Doc(document_id="notes.md", content=b"# Notes\n"))
    world.put(Doc(document_id="notes.md", content=b"   \n"))
    world.store.flush()

    assert [name for name in world.api.calls if name.startswith("Batch")] == [BATCH_DELETE]
    assert world.api.documents == {}


def test_a_skipped_document_with_an_access_list_isnt_counted_as_left_without_one(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    world.put(Doc(document_id="empty.md", content=b"  \n", access_control=AccessControl(entries=(USER,))))

    with caplog.at_level(logging.WARNING):
        world.store.flush()

    assert [record for record in caplog.records if record.getMessage() == "access_control_lists_not_written"] == []


# Access control


def test_a_document_without_an_access_list_in_an_access_control_enabled_bedrock_destination_has_no_list(
    caplog: pytest.LogCaptureFixture,
) -> None:
    world = World(StoreSetup(access_control_enabled=True))

    with caplog.at_level(logging.WARNING):
        world.put(Doc())
        world.store.flush()

    assert "accessControlList" not in world.metadata(f"{prefix(Doc())}README.md.metadata.json")
    assert [record for record in caplog.records if record.levelno == logging.WARNING] == []


def test_a_bedrock_destination_without_access_control_writes_the_document_without_a_list_and_warns_once(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    world.put(Doc(document_id="a.md", access_control=AccessControl(entries=(USER,))))
    world.put(Doc(document_id="b.md", access_control=AccessControl(entries=(USER,))))

    with caplog.at_level(logging.WARNING):
        world.store.flush()

    assert "accessControlList" not in world.metadata(f"{prefix(Doc(document_id='a.md'))}a.md.metadata.json")
    [warning] = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert warning.document_count == 2  # type: ignore[attr-defined]


def test_a_group_entry_on_an_access_control_enabled_bedrock_destination_raises_before_any_s3_call() -> None:
    world = World(StoreSetup(access_control_enabled=True))

    with pytest.raises(UnsupportedAccessControlError):
        world.put(Doc(access_control=AccessControl(entries=(USER, GROUP))))

    assert world.s3.calls == []
