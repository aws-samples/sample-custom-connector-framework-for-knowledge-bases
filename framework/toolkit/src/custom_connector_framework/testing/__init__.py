"""Run a connector implementation against in-memory fakes, with no AWS account.

`run_implementation` runs the implementation through the job's own `JobRunner`, wired to
`FakeCcfApi`, `FakeS3`, and fake STS and Secrets Manager clients, and returns a `TestRun` with the
exit code, the store's final report, the records and files the run left, the framework calls it
made, and what it wrote to stdout. A test seeds stored documents, collections, and checkpoints
with `SeedState`.
"""

from __future__ import annotations

import signal
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

from ccf_api_models.identifiers import CollectionIdentity, SourceIdKind
from ccf_api_models.job_container import JobExitCode
from ccf_api_models.models import (
    Collection,
    Connector,
    ConnectorStatus,
    ContainerProperties,
    Destination,
    DestinationResources,
    DestinationStatus,
    DestinationType,
    Job,
    JobStatus,
    ResourceRequirements,
    SourceAccess,
    StoredDocument,
)
from pydantic import BaseModel, ConfigDict, SecretStr

from custom_connector_framework.client.pages import Pages
from custom_connector_framework.client.send_deadline import SendDeadline
from custom_connector_framework.implementation import ConnectorImplementation
from custom_connector_framework.log_lines import JobLogFields
from custom_connector_framework.runner.file_sweeper import FileSweeper
from custom_connector_framework.runner.job_inputs_reader import JobIdentity, JobInputs, JobInputsReader
from custom_connector_framework.runner.job_runner import JobRunner
from custom_connector_framework.runner.secret_reader import SecretReader
from custom_connector_framework.runner.source_access import SourceAccess as SourceAccessOpener
from custom_connector_framework.runner.termination_signal import TerminationSignal
from custom_connector_framework.store.models import StoreReport
from custom_connector_framework.testing.fake_api import FakeCcfApi
from custom_connector_framework.testing.fake_aws import FakeSecretsManager, FakeSession, FakeSessions, FakeSts
from custom_connector_framework.testing.fake_s3 import FakeS3
from custom_connector_framework.testing.stdout_capture import LogEvent, StdoutCapture

if TYPE_CHECKING:
    from mypy_boto3_sts.client import STSClient

_SEED_TIME = 1_600_000_000.0
_SERVICE_ACCOUNT = "444455556666"
_SECRET_ARN_SUFFIX = ":secret:connector-secret"  # noqa: S105 - an ARN segment, not a secret
_SOURCE_ROLE = "CcfSourceRole-test"
_EXTERNAL_ID = "ccf-ext-00000000000000000000000000000000"


class SeedDocument(BaseModel):
    """A document record the framework holds before the run, by its source IDs."""

    model_config = ConfigDict(frozen=True)

    document_id: str
    collection_id: str | None = None
    source_version: str | None = None
    content_hash: str = "sha256:seeded"


class SeedCollection(BaseModel):
    """A collection the framework holds before the run, by its source ID (None for the default collection)."""

    model_config = ConfigDict(frozen=True)

    collection_id: str | None
    checkpoint: str | None = None


class SeedState(BaseModel):
    """Records and checkpoints to load into `FakeCcfApi` before the run."""

    model_config = ConfigDict(frozen=True)

    documents: tuple[SeedDocument, ...] = ()
    collections: tuple[SeedCollection, ...] = ()


class RunImplementationRequest(BaseModel):
    """The implementation to run and the job it runs for."""

    model_config = ConfigDict(frozen=True)

    implementation: type[ConnectorImplementation]
    connector_configuration: str | None = None
    job_configuration: str | None = None
    secret: str | None = None
    """The source-access secret's value; None runs the job with no secret."""
    seed: SeedState = SeedState()
    destination_type: DestinationType = DestinationType.BEDROCK
    access_control_enabled: bool = False
    region: str = "us-east-1"
    tenant_account_id: str = "111122223333"
    destination_id: str = "dst-7c41e9a2b5d0"
    connector_id: str = "cc-e020d65d76c0"
    job_id: str = "ccj-5b8e0d2c41f9"

    @property
    def tenant_arn_prefix(self) -> str:
        return f"arn:aws:ccf:{self.region}:{self.tenant_account_id}"

    @property
    def bucket_name(self) -> str:
        return f"ccf-{self.destination_id}-{self.tenant_account_id}-{self.region}"


class TestDocument(BaseModel):
    """A document record the run left, by its source IDs."""

    __test__ = False  # pytest: a result type, not a test class
    model_config = ConfigDict(frozen=True)

    document_id: str
    collection_id: str | None
    source_version: str | None
    content_hash: str


class TestCollection(BaseModel):
    """A collection the run left, by its source ID (None for the default collection), with its checkpoint."""

    __test__ = False  # pytest: a result type, not a test class
    model_config = ConfigDict(frozen=True)

    collection_id: str | None
    checkpoint: str | None


class TestRun(BaseModel):
    """What one run did."""

    __test__ = False  # pytest: a result type, not a test class
    model_config = ConfigDict(frozen=True)

    exit_code: JobExitCode
    report: StoreReport
    """The store's final `get_report()`; empty when the job failed before its store was built."""
    documents: tuple[TestDocument, ...]
    collections: tuple[TestCollection, ...]
    files: Mapping[str, bytes]
    """Every key in the destination bucket and its body."""
    api_calls: tuple[str, ...]
    """The name of each framework operation the run called, in order."""
    log_events: tuple[LogEvent, ...]


def run_implementation(request: RunImplementationRequest) -> TestRun:
    """Run one job through `JobRunner` and return what it did; `SIGTERM`'s handler is restored after."""
    api = _seeded_api(request)
    secret_arn = f"arn:aws:secretsmanager:{request.region}:{request.tenant_account_id}{_SECRET_ARN_SUFFIX}"
    secrets = FakeSecretsManager({} if request.secret is None else {secret_arn: request.secret})
    api.job_inputs = _job_inputs(request)
    pages = Pages(api)
    runner = JobRunner(
        JobIdentity(
            tenant_arn_prefix=request.tenant_arn_prefix,
            region=request.region,
            destination_id=request.destination_id,
            connector_id=request.connector_id,
            job_id=request.job_id,
        ),
        inputs=JobInputsReader(api),
        source_access=SourceAccessOpener(
            cast("STSClient", FakeSts()), FakeSessions({"s3": api.s3, "secretsmanager": secrets})
        ),
        secrets=SecretReader(),
        api=api,
        pages=pages,
        sweeper=FileSweeper(pages),
        termination=TerminationSignal(),
        send_deadline=SendDeadline(time.monotonic),
    )
    capture = StdoutCapture(JobLogFields(job_id=request.job_id, connector_id=request.connector_id))
    previous_handler = signal.getsignal(signal.SIGTERM)
    try:
        with capture:
            outcome = runner.run(request.implementation)
    finally:
        signal.signal(signal.SIGTERM, previous_handler)
    return TestRun(
        exit_code=outcome.exit_code,
        report=outcome.report,
        documents=_test_documents(api),
        collections=tuple(_test_collection(collection) for collection in api.collections.values()),
        files=api.s3.files(),
        api_calls=tuple(api.calls),
        log_events=capture.log_events(),
    )


def _job_inputs(request: RunImplementationRequest) -> JobInputs:
    """The job, custom connector, and destination `FakeCcfApi` answers the runner's reads with."""
    destination_arn = f"{request.tenant_arn_prefix}:destination/{request.destination_id}"
    connector_arn = f"{destination_arn}/custom-connector/{request.connector_id}"
    account, region = request.tenant_account_id, request.region
    return JobInputs(
        job=Job(
            job_id=request.job_id,
            arn=f"{connector_arn}/job/{request.job_id}",
            status=JobStatus.RUNNING,
            created_at=_SEED_TIME,
            updated_at=_SEED_TIME,
            job_configuration=_secret(request.job_configuration),
        ),
        connector=Connector(
            connector_id=request.connector_id,
            arn=connector_arn,
            destination_id=request.destination_id,
            name="test-connector",
            status=ConnectorStatus.IN_USE,
            created_at=_SEED_TIME,
            updated_at=_SEED_TIME,
            runtime_role_arn=f"arn:aws:iam::{_SERVICE_ACCOUNT}:role/ccf-runtime/CcfConnectorRuntime-{'0' * 32}",
            container_properties=ContainerProperties(
                image_uri=f"{account}.dkr.ecr.{region}.amazonaws.com/ccf-connectors/test:1",
                command=["ccf-connector", "run", "connector:Test"],
                resource_requirements=ResourceRequirements(cpu=1.0, memory=2048),
                timeout=3600,
            ),
            connector_configuration=_secret(request.connector_configuration),
            source_access=SourceAccess(
                role_arn=f"arn:aws:iam::{account}:role/{_SOURCE_ROLE}",
                secret_arn=None
                if request.secret is None
                else f"arn:aws:secretsmanager:{region}:{account}{_SECRET_ARN_SUFFIX}",
                external_id=_EXTERNAL_ID,
            ),
        ),
        destination=Destination(
            destination_id=request.destination_id,
            arn=destination_arn,
            name="test-destination",
            type=request.destination_type,
            access_control_enabled=request.access_control_enabled,
            status=DestinationStatus.ACTIVE,
            role_arn=f"arn:aws:iam::{account}:role/ccf/destinations/CcfDestinationRole-test",
            destination_resources=DestinationResources(
                bucket_arn=f"arn:aws:s3:::{request.bucket_name}", bucket_name=request.bucket_name
            ),
            created_at=_SEED_TIME,
            updated_at=_SEED_TIME,
        ),
    )


def _secret(value: str | None) -> SecretStr | None:
    return None if value is None else SecretStr(value)


def _seeded_api(request: RunImplementationRequest) -> FakeCcfApi:
    api = FakeCcfApi()
    seed = request.seed
    for collection in seed.collections:
        collection_id = CollectionIdentity.of(collection.collection_id).collection_id
        api.collections[collection_id] = Collection(
            collection_id=collection_id,
            source_collection_id=collection.collection_id,
            checkpoint_data=_secret(collection.checkpoint),
            created_at=_SEED_TIME,
            updated_at=_SEED_TIME,
        )
    for document in seed.documents:
        collection_id = CollectionIdentity.of(document.collection_id).collection_id
        document_id = SourceIdKind.DOCUMENT.of(document.document_id)
        api.collections.setdefault(
            collection_id,
            Collection(
                collection_id=collection_id,
                source_collection_id=document.collection_id,
                created_at=_SEED_TIME,
                updated_at=_SEED_TIME,
            ),
        )
        api.documents[(collection_id, document_id)] = StoredDocument(
            collection_id=collection_id,
            document_id=document_id,
            source_document_id=document.document_id,
            content_hash=document.content_hash,
            source_version=document.source_version,
            created_at=_SEED_TIME,
            updated_at=_SEED_TIME,
        )
    return api


def _test_documents(api: FakeCcfApi) -> tuple[TestDocument, ...]:
    """Documents don't carry their collection's source ID, so it comes from the collection record."""
    return tuple(
        TestDocument(
            document_id=stored.source_document_id,
            collection_id=api.collections[stored.collection_id].source_collection_id,
            source_version=stored.source_version,
            content_hash=stored.content_hash,
        )
        for stored in api.documents.values()
    )


def _test_collection(collection: Collection) -> TestCollection:
    checkpoint = collection.checkpoint_data
    return TestCollection(
        collection_id=collection.source_collection_id,
        checkpoint=None if checkpoint is None else checkpoint.get_secret_value(),
    )


__all__ = [
    "FakeCcfApi",
    "FakeS3",
    "FakeSecretsManager",
    "FakeSession",
    "FakeSessions",
    "FakeSts",
    "LogEvent",
    "RunImplementationRequest",
    "SeedCollection",
    "SeedDocument",
    "SeedState",
    "StdoutCapture",
    "TestCollection",
    "TestDocument",
    "TestRun",
    "run_implementation",
]
