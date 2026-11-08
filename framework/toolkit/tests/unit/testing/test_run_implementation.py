"""`testing.run_implementation`: a sample implementation runs with no AWS account, and the run's state is readable."""

from __future__ import annotations

import io
import json
import logging
from dataclasses import dataclass

import pytest
from ccf_api_models.identifiers import CollectionIdentity, SourceIdKind
from ccf_api_models.job_container import JobExitCode
from ccf_api_models.models import (
    BatchDeleteCustomConnectorCollectionsRequest,
    BatchPutCustomConnectorDocumentsRequest,
    Collection,
    CollectionReference,
    DocumentRecord,
)
from pydantic import BaseModel

from custom_connector_framework import (
    ConnectorImplementation,
    DeleteCollectionRequest,
    DeleteDocumentRequest,
    DocumentStore,
    GetCheckpointRequest,
    JobContext,
    PutDocumentRequest,
    SaveCheckpointRequest,
)
from custom_connector_framework.client import CcfApiError
from custom_connector_framework.log_lines import JobLogFields
from custom_connector_framework.testing import (
    FakeCcfApi,
    RunImplementationRequest,
    SeedCollection,
    SeedDocument,
    SeedState,
    StdoutCapture,
    TestCollection,
    TestDocument,
    TestRun,
    run_implementation,
)

REPOSITORY = "630587223"
TOKEN = "ghp_" + "1" * 36
logger = logging.getLogger("sample_connector")


class PushedAt(BaseModel):
    pushed_at: str


class Sample(ConnectorImplementation):
    """Writes one document, deletes a seeded one, saves a checkpoint, logs, and prints."""

    def run(self) -> None:
        previous = self.store.get_checkpoint(GetCheckpointRequest(checkpoint_type=PushedAt, collection_id=REPOSITORY))
        logger.info("repository_read", extra={"previous_pushed_at": None if previous is None else previous.pushed_at})
        print("sample printed line")
        self.store.put(
            PutDocumentRequest(
                document_id=f"{REPOSITORY}/README.md",
                file_name="README.md",
                content=io.BytesIO(b"# Hello"),
                collection_id=REPOSITORY,
            )
        )
        self.store.delete(DeleteDocumentRequest(document_id=f"{REPOSITORY}/OLD.md", collection_id=REPOSITORY))
        self.store.save_checkpoint(SaveCheckpointRequest(checkpoint='{"pushed_at": "b"}', collection_id=REPOSITORY))


class ReadsOneDeletesOne(ConnectorImplementation):
    """Reads the default checkpoint and saves nothing, and deletes the seeded repository."""

    def run(self) -> None:
        self.store.get_checkpoint(GetCheckpointRequest(checkpoint_type=PushedAt))
        self.store.delete_collection(DeleteCollectionRequest(collection_id=REPOSITORY))


class Fails(ConnectorImplementation):
    def run(self) -> None:
        self.store.put(PutDocumentRequest(document_id="a.md", file_name="a.md", content=io.BytesIO(b"a")))
        raise RuntimeError("sample failure")


class PutsThenDeletesOne(ConnectorImplementation):
    """Puts a document and then deletes it in the same run."""

    def run(self) -> None:
        self.store.put(PutDocumentRequest(document_id="a.md", file_name="a.md", content=io.BytesIO(b"a")))
        self.store.delete(DeleteDocumentRequest(document_id="a.md"))


SEED = SeedState(
    documents=(SeedDocument(document_id=f"{REPOSITORY}/OLD.md", collection_id=REPOSITORY, source_version="v1"),),
    collections=(
        SeedCollection(collection_id=REPOSITORY, checkpoint='{"pushed_at": "a"}'),
        SeedCollection(collection_id=None, checkpoint='{"pushed_at": "default"}'),
    ),
)


@dataclass(frozen=True)
class SampleClass:
    """`Sample` subclassed for one test, and the stores and jobs its instances were given."""

    implementation: type[ConnectorImplementation]
    stores: list[DocumentStore]
    jobs: list[JobContext]


@pytest.fixture
def sample() -> SampleClass:
    stores: list[DocumentStore] = []
    jobs: list[JobContext] = []

    class RecordingSample(Sample):
        def run(self) -> None:
            stores.append(self.store)
            jobs.append(self.job)
            super().run()

    return SampleClass(implementation=RecordingSample, stores=stores, jobs=jobs)


def test_a_sample_implementation_runs_without_aws_and_its_report_is_the_stores_final_report(
    sample: SampleClass,
) -> None:
    run = run_implementation(RunImplementationRequest(implementation=sample.implementation, seed=SEED, secret=TOKEN))

    assert run.report == sample.stores[0].get_report()
    assert run.report.total.written == 1
    assert run.report.total.deleted == 1


def test_the_report_counts_a_collection_the_run_wrote() -> None:
    counts = run_implementation(RunImplementationRequest(implementation=Sample, seed=SEED)).report.collection_counts

    assert (counts.written, counts.unchanged, counts.deleted) == (1, 0, 0)


def test_the_report_counts_a_collection_read_and_left_unchanged_and_one_deleted() -> None:
    report = run_implementation(RunImplementationRequest(implementation=ReadsOneDeletesOne, seed=SEED)).report

    counts = report.collection_counts
    assert (counts.written, counts.unchanged, counts.deleted) == (0, 1, 1)


def test_the_run_reads_the_seeded_checkpoints_and_leaves_the_saved_one() -> None:
    run = run_implementation(RunImplementationRequest(implementation=Sample, seed=SEED))

    logged = next(event for event in run.log_events if event.message == "repository_read")
    assert json.loads(logged.line)["previous_pushed_at"] == "a"
    checkpoints = {collection.collection_id: collection.checkpoint for collection in run.collections}
    assert checkpoints == {REPOSITORY: '{"pushed_at": "b"}', None: '{"pushed_at": "default"}'}


def test_the_run_leaves_its_records_and_files_with_their_metadata() -> None:
    run = run_implementation(RunImplementationRequest(implementation=Sample, seed=SEED))

    assert [(document.document_id, document.collection_id) for document in run.documents] == [
        (f"{REPOSITORY}/README.md", REPOSITORY)
    ]
    assert any(key.endswith("/README.md") for key in run.files)
    metadata = json.loads(next(body for key, body in run.files.items() if key.endswith("README.md.metadata.json")))
    assert "metadataAttributes" in metadata


def test_log_events_hold_what_the_implementation_logged_and_printed() -> None:
    run = run_implementation(RunImplementationRequest(implementation=Sample, seed=SEED))

    logged = next(event for event in run.log_events if event.message == "repository_read")
    assert (logged.level, logged.logger) == ("INFO", "sample_connector")
    assert logged.job_id == "ccj-5b8e0d2c41f9"
    printed = next(event for event in run.log_events if event.message == "sample printed line")
    assert printed.level is None


def test_the_job_gets_the_secret_configuration_and_a_source_session(sample: SampleClass) -> None:
    run_implementation(
        RunImplementationRequest(
            implementation=sample.implementation, seed=SEED, secret=TOKEN, connector_configuration='{"org": "o"}'
        )
    )

    job = sample.jobs[0]
    assert job.secret is not None and job.secret.get_secret_value() == TOKEN
    assert job.connector_configuration is not None and job.connector_configuration.get_secret_value() == '{"org": "o"}'
    assert job.source_session.region_name == "us-east-1"


def test_an_implementation_that_raises_ends_the_job_70_and_logs_its_error() -> None:
    run = run_implementation(RunImplementationRequest(implementation=Fails))

    assert run.exit_code is JobExitCode.CONNECTOR_IMPLEMENTATION_ERROR
    [failed] = [event for event in run.log_events if event.message == "connector_implementation_failed"]
    assert failed.stack_trace is not None and "sample failure" in failed.stack_trace
    assert run.api_calls[:3] == ("GetCustomConnectorJob", "GetCustomConnector", "GetDestination")


def test_the_fake_api_rejects_a_document_id_that_isnt_the_hash_of_its_source_id() -> None:
    with pytest.raises(CcfApiError) as raised:
        FakeCcfApi().call(
            BatchPutCustomConnectorDocumentsRequest(
                destination_id="dst-7c41e9a2b5d0",
                connector_id="cc-e020d65d76c0",
                documents=[
                    DocumentRecord(document_id="doc-0000000000000000", source_document_id="a.md", content_hash="h")
                ],
            )
        )

    assert raised.value.status_code == 400


def test_the_fake_api_rejects_col_default_with_a_source_collection_id() -> None:
    with pytest.raises(CcfApiError, match="collection_id must be the collection of source_collection_id") as raised:
        FakeCcfApi().call(
            BatchPutCustomConnectorDocumentsRequest(
                destination_id="dst-7c41e9a2b5d0",
                connector_id="cc-e020d65d76c0",
                collection_id="col-default",
                source_collection_id="630587223",
                documents=[
                    DocumentRecord(document_id="doc-ca978112ca1bbdca", source_document_id="a", content_hash="h")
                ],
            )
        )

    assert raised.value.status_code == 400


def test_the_result_types_are_not_collected_as_tests() -> None:
    assert (TestDocument.__test__, TestCollection.__test__, TestRun.__test__) == (False, False, False)


def test_run_given_a_document_put_then_deleted_when_flushed_then_its_files_are_gone() -> None:
    run = run_implementation(RunImplementationRequest(implementation=PutsThenDeletesOne))

    assert run.documents == ()
    assert run.files == {}


def collection_of(source_collection_id: str) -> str:
    return CollectionIdentity.of(source_collection_id).collection_id


def test_fake_collection_delete_given_one_collection_that_holds_documents_then_deletes_none() -> None:
    api = FakeCcfApi()
    for source in ("empty-repository", REPOSITORY):
        collection_id = collection_of(source)
        api.collections[collection_id] = Collection(
            collection_id=collection_id, source_collection_id=source, created_at=1.0, updated_at=1.0
        )
    api.call(
        BatchPutCustomConnectorDocumentsRequest(
            destination_id="dst-7c41e9a2b5d0",
            connector_id="cc-e020d65d76c0",
            collection_id=collection_of(REPOSITORY),
            source_collection_id=REPOSITORY,
            documents=[
                DocumentRecord(
                    document_id=SourceIdKind.DOCUMENT.of("a.md"), source_document_id="a.md", content_hash="h"
                )
            ],
        )
    )

    with pytest.raises(CcfApiError) as raised:
        api.call(
            BatchDeleteCustomConnectorCollectionsRequest(
                destination_id="dst-7c41e9a2b5d0",
                connector_id="cc-e020d65d76c0",
                collections=[
                    CollectionReference(collection_id=collection_of("empty-repository")),
                    CollectionReference(collection_id=collection_of(REPOSITORY)),
                ],
            )
        )

    assert raised.value.status_code == 409
    assert set(api.collections) == {collection_of("empty-repository"), collection_of(REPOSITORY)}


def test_log_events_given_a_toolkit_record_when_read_then_its_fields_are_typed() -> None:
    capture = StdoutCapture(JobLogFields())
    capture.stream.write(
        json.dumps(
            {
                "timestamp": "2026-11-01T00:00:00Z",
                "level": "INFO",
                "logger": "custom_connector_framework.store.document_store",
                "message": "document_skipped",
                "document_id": "doc-1",
                "reason": "empty_content",
            }
        )
        + "\n"
    )

    [event] = capture.log_events()

    assert (event.document_id, event.reason, event.collection_id) == ("doc-1", "empty_content", None)
