"""The start-of-job sweep: keys under a prefix with no record go, and a recorded prefix keeps its newest pair."""

from __future__ import annotations

import io
import logging
from typing import TYPE_CHECKING, Any, cast

import pytest
from botocore.exceptions import ClientError
from ccf_api_models.contracts import IdentifierExamples
from ccf_api_models.identifiers import (
    CollectionIdentity,
    ConnectorRef,
    DestinationRef,
    DocumentKey,
    SourceIdKind,
    TenantContext,
)
from ccf_api_models.job_container import JobExitCode
from ccf_api_models.models import ErrorCode, StoredDocument

from custom_connector_framework.client import ApiFailure, CcfApiError
from custom_connector_framework.client.pages import Pages
from custom_connector_framework.runner.exit_codes import JobStartError
from custom_connector_framework.runner.file_sweeper import FileSweeper, SweepRequest
from custom_connector_framework.store.destination_objects import DestinationObjects
from custom_connector_framework.testing import FakeCcfApi, FakeS3
from tests.unit.runner.builders import BUCKET, CONNECTOR_ID, DESTINATION_ID, TENANT_ARN_PREFIX

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client

EXAMPLES = IdentifierExamples.load()
CONNECTOR = ConnectorRef(
    destination=DestinationRef(tenant=TenantContext.from_arn_prefix(TENANT_ARN_PREFIX), destination_id=DESTINATION_ID),
    connector_id=CONNECTOR_ID,
)
COLLECTION = CollectionIdentity.of(EXAMPLES.source_collection_id).collection_id
KEPT = SourceIdKind.DOCUMENT.of(EXAMPLES.source_document_id)
ORPHAN = SourceIdKind.DOCUMENT.of(f"{EXAMPLES.source_collection_id}/GONE.md")


def _prefix(document_id: str) -> str:
    return DocumentKey(connector_id=CONNECTOR_ID, collection_id=COLLECTION, document_id=document_id).prefix


def _api_with_record() -> FakeCcfApi:
    api = FakeCcfApi()
    api.documents[(COLLECTION, KEPT)] = StoredDocument(
        collection_id=COLLECTION,
        document_id=KEPT,
        source_document_id=EXAMPLES.source_document_id,
        content_hash="sha256:abc",
        created_at=1.0,
        updated_at=1.0,
    )
    return api


def _bucket_with(keys: list[str]) -> FakeS3:
    """A bucket holding the keys, each modified after the one before it."""
    s3 = FakeS3()
    for key in keys:
        s3.put_object(Bucket=BUCKET, Key=key, Body=io.BytesIO(b"x"))
    return s3


def _sweep(s3: Any, api: FakeCcfApi | None = None) -> None:
    objects = DestinationObjects(cast("S3Client", s3), BUCKET)
    FileSweeper(Pages(api or _api_with_record())).sweep(SweepRequest(connector=CONNECTOR, objects=objects))


def test_every_key_under_a_prefix_with_no_record_is_deleted() -> None:
    orphan = _prefix(ORPHAN)
    s3 = _bucket_with([f"{orphan}GONE.md.metadata.json", f"{orphan}GONE.md"])

    _sweep(s3)

    assert s3.objects == {}


def test_a_recorded_prefix_keeps_only_its_newest_file_and_that_files_metadata_file() -> None:
    kept = _prefix(KEPT)
    s3 = _bucket_with(
        [
            f"{kept}README.txt.metadata.json",
            f"{kept}README.txt",
            f"{kept}README.md.metadata.json",
            f"{kept}README.md",
            f"{kept}NOTES.md.metadata.json",
        ],
    )

    _sweep(s3)

    assert sorted(s3.objects) == [f"{kept}README.md", f"{kept}README.md.metadata.json"]


def test_a_recorded_prefix_with_only_a_metadata_file_loses_it() -> None:
    s3 = _bucket_with([f"{_prefix(KEPT)}README.md.metadata.json"])

    _sweep(s3)

    assert s3.objects == {}


def test_a_key_outside_any_document_prefix_is_deleted() -> None:
    s3 = _bucket_with([f"{CONNECTOR.key_prefix}stray.txt"])

    _sweep(s3)

    assert s3.objects == {}


def test_keys_of_another_custom_connector_are_not_touched() -> None:
    other = "connector_id=cc-000000000000/collection_id=col-default/document_id=doc-0000000000000000/a.md"
    s3 = _bucket_with([other])

    _sweep(s3)

    assert list(s3.objects) == [other]


def test_the_sweep_logs_counts_and_document_ids_never_keys(caplog: pytest.LogCaptureFixture) -> None:
    kept, orphan = _prefix(KEPT), _prefix(ORPHAN)
    s3 = _bucket_with([f"{kept}README.txt", f"{kept}README.md", f"{orphan}GONE.md"])
    caplog.set_level(logging.INFO)

    _sweep(s3)

    records = [record for record in caplog.records if record.name.endswith("file_sweeper")]
    counts = {record.__dict__.get("document_id"): record.__dict__.get("deleted_key_count") for record in records}
    assert counts == {KEPT: 1, ORPHAN: 1, None: 2}
    logged = " ".join(f"{record.getMessage()} {record.__dict__}" for record in records)
    assert "README" not in logged
    assert "GONE" not in logged


def test_a_key_s3_refuses_to_delete_is_counted_as_failed_and_logged_with_codes_and_no_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    orphan = _prefix(ORPHAN)
    s3 = _bucket_with([f"{orphan}GONE.md.metadata.json", f"{orphan}GONE.md"])
    s3.refused_deletes.add(f"{orphan}GONE.md")

    _sweep(s3)

    assert list(s3.objects) == [f"{orphan}GONE.md"]
    [failed] = [record for record in caplog.records if record.getMessage() == "sweep_delete_failed"]
    assert failed.levelname == "WARNING"
    assert (failed.failed_key_count, failed.error_codes) == (1, ["AccessDenied"])  # type: ignore[attr-defined]
    [finished] = [record for record in caplog.records if record.getMessage() == "sweep_finished"]
    assert (finished.deleted_key_count, finished.failed_key_count) == (1, 1)  # type: ignore[attr-defined]
    assert "GONE.md" not in caplog.text


class DenyingS3(FakeS3):
    """A bucket whose listing S3 denies to the source-access role."""

    def get_paginator(self, operation_name: str) -> Any:
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, "ListObjectsV2")


class FailingApi(FakeCcfApi):
    """A framework API whose every call fails."""

    def call(self, request: Any) -> Any:
        raise CcfApiError(ApiFailure(status_code=503, code=ErrorCode.SERVICE_UNAVAILABLE, message="unavailable"))


@pytest.mark.parametrize(
    ("s3", "api", "exit_code"),
    [
        (DenyingS3(), _api_with_record(), JobExitCode.SOURCE_ACCESS_DENIED),
        (FakeS3(), FailingApi(), JobExitCode.FRAMEWORK_API_UNAVAILABLE),
    ],
    ids=["s3-access-denied", "api-failure"],
)
def test_a_failed_sweep_ends_the_job_start_with_its_exit_code(
    s3: FakeS3, api: FakeCcfApi, exit_code: JobExitCode
) -> None:
    with pytest.raises(JobStartError) as raised:
        _sweep(s3, api)

    assert raised.value.exit_code is exit_code
