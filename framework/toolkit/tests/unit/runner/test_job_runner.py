"""The job runner: inputs, source access, the run, the flush, and exit codes."""

from __future__ import annotations

import io
import json
import logging
import os
import signal
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import ClassVar

import boto3
import pytest
from botocore.exceptions import ClientError
from ccf_api_models.identifiers import DEFAULT_COLLECTION_ID
from ccf_api_models.job_container import JobExitCode
from ccf_api_models.models import Collection, ErrorCode
from ccf_api_models.operations import Operation
from pydantic import BaseModel, SecretStr, ValidationError

from custom_connector_framework import (
    ConnectorImplementation,
    DeleteDocumentRequest,
    GetCheckpointRequest,
    JobContext,
    PutDocumentRequest,
)
from custom_connector_framework.client import ApiFailure, CcfApiError
from custom_connector_framework.client.send_deadline import SendDeadlineExceededError
from custom_connector_framework.log_lines import JobLogFields, JsonLineFormatter
from custom_connector_framework.store.document_store import DocumentStore
from tests.unit.runner.builders import (
    CONNECTOR_ID,
    DESTINATION_ID,
    EXTERNAL_ID,
    JOB_ID,
    SECRET_ARN,
    SECRET_VALUE,
    SOURCE_ROLE_ARN,
    InputsRequest,
    RunnerWorld,
)

SOURCE_COLLECTION_ID = "awslabs/private-source-name"


class SeededCheckpoint(BaseModel):
    cursor: str


def unavailable() -> CcfApiError:
    return CcfApiError(ApiFailure(status_code=503, code=ErrorCode.SERVICE_UNAVAILABLE, message="down", request_id="r"))


@dataclass(frozen=True)
class RecorderClass:
    """An implementation class built for one test, and the instances it made and the runs it did."""

    implementation: type[ConnectorImplementation]
    seen: list[ConnectorImplementation]
    runs: list[None]


@pytest.fixture
def recorder() -> RecorderClass:
    """A fresh implementation class per test, so no test sees another's instances."""
    seen: list[ConnectorImplementation] = []
    runs: list[None] = []

    class Recorder(ConnectorImplementation):
        def __init__(self, job: JobContext, store: DocumentStore) -> None:
            super().__init__(job, store)
            seen.append(self)

        def run(self) -> None:
            runs.append(None)

    return RecorderClass(implementation=Recorder, seen=seen, runs=runs)


class PutsThenDeletesThenRaises(ConnectorImplementation):
    def run(self) -> None:
        self.store.put(PutDocumentRequest(document_id="README.md", file_name="README.md", content=io.BytesIO(b"hi")))
        self.store.delete(DeleteDocumentRequest(document_id="OLD.md"))
        raise RuntimeError("sentinel-failure")


class PutsIntoASourceCollection(ConnectorImplementation):
    def run(self) -> None:
        self.store.put(
            PutDocumentRequest(
                document_id="README.md",
                file_name="README.md",
                content=io.BytesIO(b"hi"),
                collection_id=SOURCE_COLLECTION_ID,
            )
        )


class PutsThenDeletesThenWaitsForSigterm(ConnectorImplementation):
    def run(self) -> None:
        self.store.put(PutDocumentRequest(document_id="README.md", file_name="README.md", content=io.BytesIO(b"hi")))
        self.store.delete(DeleteDocumentRequest(document_id="OLD.md"))
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(5)
        raise AssertionError("SIGTERM didn't stop run()")


class SwallowsExceptions(ConnectorImplementation):
    def run(self) -> None:
        try:
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(5)
        except Exception:  # noqa: S110 - proves JobTerminatedError isn't an Exception
            pass


class PutsOneDocument(ConnectorImplementation):
    def run(self) -> None:
        self.store.put(PutDocumentRequest(document_id="README.md", file_name="README.md", content=io.BytesIO(b"hi")))


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    previous = signal.getsignal(signal.SIGTERM)
    yield
    signal.signal(signal.SIGTERM, previous)


def test_a_successful_run_exits_succeeded_and_runs_the_implementation_once(recorder: RecorderClass) -> None:
    world = RunnerWorld()

    assert world.run(recorder.implementation) is JobExitCode.SUCCEEDED
    assert len(recorder.runs) == 1


def test_run_reads_the_job_connector_and_destination_first(recorder: RecorderClass) -> None:
    world = RunnerWorld()

    world.run(recorder.implementation)

    assert world.api.calls[:3] == ["GetCustomConnectorJob", "GetCustomConnector", "GetDestination"]
    assert "GetCustomConnectorCheckpoint" not in world.api.calls


def test_run_assumes_the_source_access_role_with_its_external_id_for_an_hour(recorder: RecorderClass) -> None:
    world = RunnerWorld()

    world.run(recorder.implementation)

    assert world.sts.calls == [
        {
            "RoleArn": SOURCE_ROLE_ARN,
            "RoleSessionName": f"ccf-{JOB_ID}",
            "ExternalId": EXTERNAL_ID,
            "DurationSeconds": 3600,
        }
    ]


def test_run_reads_the_secret_once(recorder: RecorderClass) -> None:
    world = RunnerWorld()

    world.run(recorder.implementation)

    assert world.secrets.calls == [SECRET_ARN]


def test_the_job_context_holds_the_job_ids(recorder: RecorderClass) -> None:
    RunnerWorld().run(recorder.implementation)

    job = recorder.seen[0].job
    assert (job.destination_id, job.connector_id, job.job_id) == (DESTINATION_ID, CONNECTOR_ID, JOB_ID)


def test_the_job_context_holds_the_secret_value(recorder: RecorderClass) -> None:
    RunnerWorld().run(recorder.implementation)

    secret = recorder.seen[0].job.secret
    assert secret is not None and secret.get_secret_value() == SECRET_VALUE


def test_the_job_context_holds_the_connector_configuration(recorder: RecorderClass) -> None:
    RunnerWorld().run(recorder.implementation)

    configuration = recorder.seen[0].job.connector_configuration
    assert configuration is not None and configuration.get_secret_value() == '{"owner": "awslabs"}'


class ReadsTheDefaultCheckpoint(ConnectorImplementation):
    read: ClassVar[list[SeededCheckpoint | None]] = []

    def run(self) -> None:
        self.read.append(self.store.get_checkpoint(GetCheckpointRequest(checkpoint_type=SeededCheckpoint)))


def test_an_implementation_reads_the_seeded_default_checkpoint_through_the_store() -> None:
    world = RunnerWorld()
    world.api.collections[DEFAULT_COLLECTION_ID] = Collection(
        collection_id=DEFAULT_COLLECTION_ID,
        source_collection_id=None,
        checkpoint_data=SecretStr('{"cursor": "page-7"}'),
        created_at=1.0,
        updated_at=1.0,
    )
    ReadsTheDefaultCheckpoint.read.clear()

    world.run(ReadsTheDefaultCheckpoint)

    assert ReadsTheDefaultCheckpoint.read == [SeededCheckpoint(cursor="page-7")]


def test_source_access_session_builds_the_secret_and_s3_clients_and_is_the_job_contexts_session(
    recorder: RecorderClass,
) -> None:
    world = RunnerWorld()

    world.run(recorder.implementation)

    [source_session] = world.sessions.built
    assert set(source_session.requested) == {"secretsmanager", "s3"}
    assert recorder.seen[0].job.source_session is source_session


def test_no_secret_arn_reads_no_secret(recorder: RecorderClass) -> None:
    world = RunnerWorld(inputs=InputsRequest(secret_arn=None))

    world.run(recorder.implementation)

    assert world.secrets.calls == []
    assert recorder.seen[0].job.secret is None


def test_run_raising_sends_puts_then_deletes_and_exits_with_the_implementation_error_code() -> None:
    world = RunnerWorld()

    exit_code = world.run(PutsThenDeletesThenRaises)

    assert exit_code is JobExitCode.CONNECTOR_IMPLEMENTATION_ERROR
    sent = [call for call in world.api.calls if call.startswith("Batch")]
    assert sent == ["BatchPutCustomConnectorDocuments", "BatchDeleteCustomConnectorDocuments"]


def test_sigterm_stops_run_sends_the_queued_records_and_exits_with_the_terminated_code() -> None:
    world = RunnerWorld()
    started = time.monotonic()

    exit_code = world.run(PutsThenDeletesThenWaitsForSigterm)

    assert exit_code is JobExitCode.JOB_TERMINATED
    assert time.monotonic() - started < 5
    sent = [call for call in world.api.calls if call.startswith("Batch")]
    assert sent == ["BatchPutCustomConnectorDocuments", "BatchDeleteCustomConnectorDocuments"]


def test_run_given_sigterm_when_flushing_then_the_send_deadline_ends_25_seconds_after_it() -> None:
    world = RunnerWorld()

    world.run(PutsThenDeletesThenWaitsForSigterm)

    world.send_deadline.check(25.0)
    with pytest.raises(SendDeadlineExceededError):
        world.send_deadline.check(25.1)


def test_run_given_a_failure_other_than_sigterm_when_flushing_then_no_send_deadline_starts() -> None:
    world = RunnerWorld()

    world.run(PutsThenDeletesThenRaises)

    world.send_deadline.check(10_000.0)


def test_a_successful_run_whose_final_send_fails_still_reports_and_exits_76(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    world = RunnerWorld()
    world.api.failures[Operation.BATCH_PUT_CUSTOM_CONNECTOR_DOCUMENTS] = unavailable()

    exit_code = world.run(PutsOneDocument)

    assert exit_code is JobExitCode.FRAMEWORK_API_UNAVAILABLE
    messages = [(record.levelname, record.getMessage()) for record in caplog.records]
    assert ("ERROR", "queued_records_not_sent") in messages
    assert ("INFO", "job_reported") in messages


def test_a_failed_send_after_a_failed_run_keeps_the_implementation_error_code() -> None:
    world = RunnerWorld()
    world.api.failures[Operation.BATCH_PUT_CUSTOM_CONNECTOR_DOCUMENTS] = unavailable()

    assert world.run(PutsThenDeletesThenRaises) is JobExitCode.CONNECTOR_IMPLEMENTATION_ERROR


def test_the_report_line_carries_the_job_id_and_no_source_collection_id(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)

    RunnerWorld().run(PutsIntoASourceCollection)

    [reported] = [record for record in caplog.records if record.getMessage() == "job_reported"]
    line = JsonLineFormatter(JobLogFields(job_id=JOB_ID, connector_id=CONNECTOR_ID)).format(reported)
    assert json.loads(line)["job_id"] == JOB_ID
    assert SOURCE_COLLECTION_ID not in line


def test_sigterm_isnt_swallowed_by_an_implementations_except_exception() -> None:
    assert RunnerWorld().run(SwallowsExceptions) is JobExitCode.JOB_TERMINATED


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "no"}}, "AssumeRole")


@pytest.mark.parametrize(
    ("error_code", "expected"),
    [("AccessDenied", JobExitCode.SOURCE_ACCESS_DENIED), ("Throttling", JobExitCode.SOURCE_ACCESS_FAILED)],
)
def test_assume_role_failures_exit_before_run(error_code: str, expected: JobExitCode, recorder: RecorderClass) -> None:
    world = RunnerWorld()
    world.sts.error = _client_error(error_code)

    assert world.run(recorder.implementation) is expected
    assert recorder.seen == []


def test_a_secret_read_failure_exits_before_run(recorder: RecorderClass) -> None:
    world = RunnerWorld()
    world.secrets.secrets.clear()

    assert world.run(recorder.implementation) is JobExitCode.SECRET_UNAVAILABLE
    assert recorder.seen == []


def test_a_framework_api_failure_on_the_first_read_exits_before_run(recorder: RecorderClass) -> None:
    world = RunnerWorld()
    world.api.job_inputs = None

    assert world.run(recorder.implementation) is JobExitCode.FRAMEWORK_API_UNAVAILABLE
    assert recorder.seen == []
    assert world.sts.calls == []


def test_a_destination_without_a_bucket_name_exits_framework_api_unavailable_before_run(
    recorder: RecorderClass,
) -> None:
    world = RunnerWorld(inputs=InputsRequest(bucket_name=None))

    assert world.run(recorder.implementation) is JobExitCode.FRAMEWORK_API_UNAVAILABLE
    assert recorder.seen == []


def test_an_s3_access_denial_in_the_sweep_exits_source_access_denied_before_run(
    monkeypatch: pytest.MonkeyPatch, recorder: RecorderClass
) -> None:
    world = RunnerWorld()

    def denied(operation_name: str) -> object:
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "ListObjectsV2")

    monkeypatch.setattr(world.s3, "get_paginator", denied)

    assert world.run(recorder.implementation) is JobExitCode.SOURCE_ACCESS_DENIED
    assert recorder.seen == []


def test_a_framework_api_failure_in_the_sweep_exits_framework_api_unavailable_before_run(
    recorder: RecorderClass,
) -> None:
    world = RunnerWorld()
    world.api.failures[Operation.LIST_CUSTOM_CONNECTOR_DOCUMENTS] = unavailable()

    assert world.run(recorder.implementation) is JobExitCode.FRAMEWORK_API_UNAVAILABLE
    assert recorder.seen == []


def test_job_context_and_store_expose_no_framework_api_client(recorder: RecorderClass) -> None:
    """In a job, the framework API client holds the runtime role session; nothing public reaches it.

    This keeps the implementation from using the runtime role by accident. It isn't an isolation
    boundary: code in the container can still read the task role's credentials, so the runtime
    role's permissions boundary and policy are the control.
    """
    world = RunnerWorld()

    world.run(recorder.implementation)

    implementation = recorder.seen[0]
    reachable = _public_values(implementation.job) + _public_values(implementation.store)
    assert all(value is not world.api for value in reachable)
    assert all(value is not world.sts for value in reachable)


def _public_values(root: object) -> list[object]:
    """Every object reachable from root through public attributes, four levels deep."""
    found: list[object] = []
    frontier = [root]
    for _ in range(4):
        found.extend(frontier)
        frontier = [member for value in frontier for name, member in _members(value) if not name.startswith("_")]
    return found


def _members(value: object) -> list[tuple[str, object]]:
    try:
        return list(vars(value).items())
    except TypeError:
        return []


def test_a_job_context_rejects_a_plain_string_secret() -> None:
    with pytest.raises(ValidationError, match="secret"):
        JobContext(
            destination_id=DESTINATION_ID,
            connector_id=CONNECTOR_ID,
            job_id=JOB_ID,
            connector_configuration=None,
            job_configuration=None,
            secret="plain-text-secret",  # noqa: S106 - a fake value the model must refuse
            source_session=boto3.Session(region_name="us-east-1"),
        )
