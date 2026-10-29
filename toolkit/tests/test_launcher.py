"""Tests for the generic service-configured connector launcher."""

import os
import sys
from collections.abc import Iterator, Mapping
from types import ModuleType
from typing import Any, cast

import boto3
import pytest
from ccf_api_models.models import (
    CcfServiceReference,
    CommandItem,
    ConnectorStatus,
    ContainerProperties,
    CustomConnectorDetail,
    DeploymentDestination,
    DestinationMode,
    GetCustomConnectorJobRequest,
    GetCustomConnectorJobResponseContent,
    GetCustomConnectorRequest,
    GetCustomConnectorResponseContent,
    JobDetail,
    JobStatus,
    ListCustomConnectorDocumentsRequest,
    ListCustomConnectorDocumentsResponseContent,
    ResourceRequirements,
    SourceAccessConfiguration,
)
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

from custom_connector_framework import EmptyJobConfiguration
from custom_connector_framework import launcher as launcher_module
from custom_connector_framework.client import CcfClient
from custom_connector_framework.definition import ConnectorContext, ConnectorDefinition
from custom_connector_framework.launcher import (
    ConnectorLauncher,
    RuntimeEnvironment,
    SourceSessionProvider,
    SourceSessionRequest,
    main,
)
from custom_connector_framework.models.document import Document
from tests.conftest import make_document


class GlobalConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    repository: str


class JobConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    full_snapshot: bool


class FakeStsClient:
    def __init__(self) -> None:
        self.requests: list[dict[str, str]] = []

    def assume_role(self, **request: str) -> dict[str, dict[str, str]]:
        self.requests.append(request)
        return {
            "Credentials": {
                "AccessKeyId": "temporary-access-key",
                "SecretAccessKey": "temporary-secret-key",
                "SessionToken": "temporary-session-token",
            }
        }


class FakeS3Client:
    def __init__(self) -> None:
        self.put_requests: list[dict[str, Any]] = []
        self.delete_requests: list[dict[str, Any]] = []

    def put_object(self, **request: Any) -> None:
        self.put_requests.append(request)

    def delete_object(self, **request: Any) -> None:
        self.delete_requests.append(request)


class FakeControlSession:
    def __init__(self, region_name: str | None = "us-west-2") -> None:
        self.region_name = region_name
        self.sts = FakeStsClient()
        self.s3 = FakeS3Client()
        self.requested_services: list[str] = []

    def client(self, service_name: str) -> Any:
        self.requested_services.append(service_name)
        if service_name == "sts":
            return self.sts
        if service_name == "s3":
            return self.s3
        raise AssertionError("unexpected AWS service client")


class FakeCcfClient(CcfClient):
    def __init__(
        self,
        connector_response: GetCustomConnectorResponseContent,
        job_response: GetCustomConnectorJobResponseContent,
    ) -> None:
        self.connector_response = connector_response
        self.job_response = job_response
        self.connector_requests: list[GetCustomConnectorRequest] = []
        self.job_requests: list[GetCustomConnectorJobRequest] = []
        self.list_requests: list[ListCustomConnectorDocumentsRequest] = []

    def get_custom_connector(self, request: GetCustomConnectorRequest) -> GetCustomConnectorResponseContent:
        self.connector_requests.append(request)
        return self.connector_response

    def get_custom_connector_job(self, request: GetCustomConnectorJobRequest) -> GetCustomConnectorJobResponseContent:
        self.job_requests.append(request)
        return self.job_response

    def list_custom_connector_documents(
        self,
        request: ListCustomConnectorDocumentsRequest,
    ) -> ListCustomConnectorDocumentsResponseContent:
        self.list_requests.append(request)
        return ListCustomConnectorDocumentsResponseContent(documents=[])

    def batch_put_custom_connector_documents(self, request: Any) -> None:
        return None

    def batch_delete_custom_connector_documents(self, request: Any) -> None:
        return None


class RecordingEnvironment(Mapping[str, str]):
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values
        self.read_keys: list[str] = []

    def __getitem__(self, key: str) -> str:
        self.read_keys.append(key)
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


def _connector_response(
    mode: DestinationMode,
    connector_configuration: str = '{"repository":"owner/repo"}',
    service_region: str = "us-east-1",
) -> GetCustomConnectorResponseContent:
    connector = CustomConnectorDetail(
        connector_id="connector-1",
        arn="arn:aws:ccf:us-east-1:123456789012:connector/connector-1",
        name="connector-1",
        created_at=1.0,
        updated_at=2.0,
        status=ConnectorStatus.IN_USE,
        container_properties=ContainerProperties(
            image_uri="123456789012.dkr.ecr.us-east-1.amazonaws.com/connectors/image:latest",
            command=[CommandItem(root="ccf-connector"), CommandItem(root="run"), CommandItem(root="sample:definition")],
            resource_requirements=ResourceRequirements(cpu=1, memory=512),
            timeout=60,
        ),
        connector_configuration=SecretStr(connector_configuration),
        source_access=SourceAccessConfiguration(
            role_arn="arn:aws:iam::210987654321:role/ExactSourceRole",
            external_id="exact-external-id",
        ),
    )
    service_reference = CcfServiceReference(
        endpoint="https://ccf.example.test/stage",
        region=service_region,
        service_principal="ccf.example.test",
        service_account_id="123456789012",
        destination=DeploymentDestination(
            mode=mode,
            bucket_arn="arn:aws:s3:::service-destination-bucket",
            object_prefix="service-prefix",
        ),
    )
    return GetCustomConnectorResponseContent(connector=connector, service_reference=service_reference)


def _job_response(job_configuration: str = '{"full_snapshot":true}') -> GetCustomConnectorJobResponseContent:
    return GetCustomConnectorJobResponseContent(
        job=JobDetail(
            job_id="job-1",
            connector_id="connector-1",
            status=JobStatus.RUNNING,
            job_configuration=SecretStr(job_configuration),
            created_at=1.0,
            updated_at=2.0,
        )
    )


def _environment() -> RuntimeEnvironment:
    return RuntimeEnvironment(
        endpoint="https://ccf.example.test/stage",
        connector_id="connector-1",
        tenant_arn_prefix="arn:aws:ccf:us-east-1:123456789012",
        job_id="job-1",
        region="us-east-1",
    )


def _install_definition(
    monkeypatch: pytest.MonkeyPatch,
    documents: Any,
) -> str:
    module = ModuleType("test_connector_definition")
    module.definition = ConnectorDefinition(
        connector_configuration_model=GlobalConfiguration,
        job_configuration_model=JobConfiguration,
        documents=documents,
    )
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return f"{module.__name__}:definition"


def test_runtime_environment_reads_only_five_service_values() -> None:
    values = {
        "CUSTOM_CONNECTOR_FRAMEWORK_API_ENDPOINT": "https://ccf.example.test/stage",
        "CUSTOM_CONNECTOR_FRAMEWORK_CUSTOM_CONNECTOR_ID": "connector-1",
        "CUSTOM_CONNECTOR_FRAMEWORK_CUSTOM_CONNECTOR_ARN_PREFIX": "arn:aws:ccf:us-east-1:123456789012",
        "CUSTOM_CONNECTOR_JOB_ID": "job-1",
        "AWS_REGION": "us-east-1",
        "PROVIDER": "must-not-read",
        "DESTINATION": "must-not-read",
        "INGESTION_BUCKET_NAME": "must-not-read",
        "CONNECTOR_PREFIX": "must-not-read",
        "SOURCE_ACCESS_ROLE_ARN": "must-not-read",
        "SOURCE_ACCESS_EXTERNAL_ID": "must-not-read",
    }
    environment = RecordingEnvironment(values)

    parsed = RuntimeEnvironment.from_environ(environment)

    assert parsed.connector_id == "connector-1"
    assert parsed.region == "us-east-1"
    assert parsed.tenant_arn_prefix == "arn:aws:ccf:us-east-1:123456789012"
    assert environment.read_keys == [
        "CUSTOM_CONNECTOR_FRAMEWORK_API_ENDPOINT",
        "CUSTOM_CONNECTOR_FRAMEWORK_CUSTOM_CONNECTOR_ID",
        "CUSTOM_CONNECTOR_FRAMEWORK_CUSTOM_CONNECTOR_ARN_PREFIX",
        "CUSTOM_CONNECTOR_JOB_ID",
        "AWS_REGION",
    ]


def test_runtime_environment_requires_the_delegated_tenant_prefix() -> None:
    values = {
        "CUSTOM_CONNECTOR_FRAMEWORK_API_ENDPOINT": "https://ccf.example.test/stage",
        "CUSTOM_CONNECTOR_FRAMEWORK_CUSTOM_CONNECTOR_ID": "connector-1",
        "CUSTOM_CONNECTOR_JOB_ID": "job-1",
        "AWS_REGION": "us-east-1",
    }

    with pytest.raises(RuntimeError, match="Required connector runtime configuration is missing."):
        RuntimeEnvironment.from_environ(RecordingEnvironment(values))


def test_source_session_uses_exact_role_external_id_and_no_environment_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control_session = FakeControlSession()
    session_calls: list[dict[str, str]] = []
    source_session = object()

    def capture_session(**request: str) -> object:
        session_calls.append(request)
        return source_session

    monkeypatch.setattr(boto3, "Session", capture_session)
    credential_environment_before = {
        name: os.environ.get(name) for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")
    }

    returned = SourceSessionProvider(cast(Any, control_session)).assume(
        SourceSessionRequest(
            source_access=SourceAccessConfiguration(
                role_arn="arn:aws:iam::210987654321:role/ExactSourceRole",
                external_id="exact-external-id",
            ),
            region="us-east-1",
        )
    )

    assert returned is source_session
    assert control_session.sts.requests == [
        {
            "RoleArn": "arn:aws:iam::210987654321:role/ExactSourceRole",
            "ExternalId": "exact-external-id",
            "RoleSessionName": "ccf-connector",
        }
    ]
    assert session_calls == [
        {
            "aws_access_key_id": "temporary-access-key",
            "aws_secret_access_key": "temporary-secret-key",
            "aws_session_token": "temporary-session-token",
            "region_name": "us-east-1",
        }
    ]
    assert credential_environment_before == {
        name: os.environ.get(name) for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")
    }


@pytest.mark.parametrize(
    ("mode", "expected_suffix", "expected_put_count"),
    [
        (DestinationMode.S3, ".java", 1),
        (DestinationMode.BEDROCK, ".java.txt", 2),
        (DestinationMode.QUICK, ".java.txt", 2),
    ],
)
def test_launcher_fetches_typed_config_and_derives_destination_from_service(
    monkeypatch: pytest.MonkeyPatch,
    mode: DestinationMode,
    expected_suffix: str,
    expected_put_count: int,
) -> None:
    control_session = FakeControlSession()
    source_session = cast(boto3.Session, object())
    monkeypatch.setattr(boto3, "Session", lambda **kwargs: source_session)
    captured_contexts: list[ConnectorContext[GlobalConfiguration, JobConfiguration]] = []
    source_document = make_document("repo-document", b"class App {}", filename="App.java")

    def documents(
        context: ConnectorContext[GlobalConfiguration, JobConfiguration],
    ) -> list[Document]:
        captured_contexts.append(context)
        return [source_document]

    definition_target = _install_definition(monkeypatch, documents)
    client = FakeCcfClient(_connector_response(mode), _job_response())
    launcher = ConnectorLauncher(
        environment=_environment(),
        client=client,
        control_session=cast(Any, control_session),
        source_sessions=SourceSessionProvider(cast(Any, control_session)),
    )

    result = launcher.run(definition_target)

    assert client.connector_requests == [GetCustomConnectorRequest(connector_id="connector-1")]
    assert client.job_requests == [GetCustomConnectorJobRequest(connector_id="connector-1", job_id="job-1")]
    assert captured_contexts[0].connector_configuration == GlobalConfiguration(repository="owner/repo")
    assert captured_contexts[0].job_configuration == JobConfiguration(full_snapshot=True)
    assert captured_contexts[0].source_session is source_session
    assert control_session.sts.requests[0]["RoleArn"].endswith("ExactSourceRole")
    assert control_session.sts.requests[0]["ExternalId"] == "exact-external-id"
    assert control_session.requested_services == ["sts", "s3"]
    assert len(control_session.s3.put_requests) == expected_put_count
    assert control_session.s3.put_requests[0]["Bucket"] == "service-destination-bucket"
    expected_key = f"service-prefix/repo-document/{source_document.get_checksum()}{expected_suffix}"
    assert control_session.s3.put_requests[0]["Key"] == expected_key
    assert result.documents_written == 1


def test_launcher_rejects_non_strict_configuration_model(monkeypatch: pytest.MonkeyPatch) -> None:
    class LooseConfiguration(BaseModel):
        repository: str

    module = ModuleType("loose_connector_definition")
    module.definition = ConnectorDefinition(
        connector_configuration_model=LooseConfiguration,
        job_configuration_model=JobConfiguration,
        documents=lambda context: [],
    )
    monkeypatch.setitem(sys.modules, module.__name__, module)
    client = FakeCcfClient(_connector_response(DestinationMode.S3), _job_response())
    control_session = FakeControlSession()
    launcher = ConnectorLauncher(
        environment=_environment(),
        client=client,
        control_session=cast(Any, control_session),
        source_sessions=SourceSessionProvider(cast(Any, control_session)),
    )

    with pytest.raises(TypeError, match="strict, frozen"):
        launcher.run(f"{module.__name__}:definition")

    assert control_session.requested_services == []


def test_launcher_rejects_invalid_secret_json_without_exposing_it(monkeypatch: pytest.MonkeyPatch) -> None:
    definition_target = _install_definition(monkeypatch, lambda context: [])
    client = FakeCcfClient(
        _connector_response(DestinationMode.S3, '{"repository":"private","extra_secret":"must-not-appear"}'),
        _job_response(),
    )
    control_session = FakeControlSession()
    launcher = ConnectorLauncher(
        environment=_environment(),
        client=client,
        control_session=cast(Any, control_session),
        source_sessions=SourceSessionProvider(cast(Any, control_session)),
    )

    with pytest.raises(ValueError) as caught:
        launcher.run(definition_target)

    assert str(caught.value) == "Connector configuration failed validation."
    assert "must-not-appear" not in str(caught.value)
    assert control_session.requested_services == []


def test_main_constructs_control_session_and_client_with_explicit_region(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in {
        "CUSTOM_CONNECTOR_FRAMEWORK_API_ENDPOINT": "https://ccf.example.test/stage",
        "CUSTOM_CONNECTOR_FRAMEWORK_CUSTOM_CONNECTOR_ID": "connector-1",
        "CUSTOM_CONNECTOR_FRAMEWORK_CUSTOM_CONNECTOR_ARN_PREFIX": "arn:aws:ccf:eu-west-1:123456789012",
        "CUSTOM_CONNECTOR_JOB_ID": "job-1",
        "AWS_REGION": "eu-west-1",
    }.items():
        monkeypatch.setenv(key, value)

    control_session = FakeControlSession(region_name=None)
    session_requests: list[dict[str, str]] = []
    client_requests: list[dict[str, object]] = []
    definition_targets: list[str] = []

    def create_session(**request: str) -> FakeControlSession:
        session_requests.append(request)
        return control_session

    def main_client(**request: object) -> object:
        client_requests.append(request)
        return object()

    class MainLauncher:
        def __init__(self, **request: object) -> None:
            pass

        def run(self, definition_target: str) -> None:
            definition_targets.append(definition_target)

    monkeypatch.setattr(launcher_module.boto3, "Session", create_session)
    monkeypatch.setattr(launcher_module, "_create_runtime_client", main_client)
    monkeypatch.setattr(launcher_module, "ConnectorLauncher", MainLauncher)

    assert main(["run", "sample_connector:definition"]) == 0
    assert session_requests == [{"region_name": "eu-west-1"}]
    assert client_requests == [
        {
            "endpoint": "https://ccf.example.test/stage",
            "region": "eu-west-1",
            "session": control_session,
            "tenant_arn_prefix": "arn:aws:ccf:eu-west-1:123456789012",
        }
    ]
    assert definition_targets == ["sample_connector:definition"]


def test_main_sanitizes_missing_runtime_environment(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for key in (
        "CUSTOM_CONNECTOR_FRAMEWORK_API_ENDPOINT",
        "CUSTOM_CONNECTOR_FRAMEWORK_CUSTOM_CONNECTOR_ID",
        "CUSTOM_CONNECTOR_JOB_ID",
        "AWS_REGION",
    ):
        monkeypatch.delenv(key, raising=False)

    with pytest.raises(SystemExit) as caught:
        main(["run", "private_module:definition"])

    assert caught.value.code == 1
    assert capsys.readouterr().err == "ccf-connector: connector run failed\n"


def test_empty_job_configuration_removes_empty_author_model_boilerplate() -> None:
    assert EmptyJobConfiguration.model_validate_json("{}") == EmptyJobConfiguration()

    with pytest.raises(ValidationError):
        EmptyJobConfiguration.model_validate_json('{"unexpected":"value"}')
