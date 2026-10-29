"""Reusable local runtime harness for sample connector end-to-end tests."""

from typing import Any

import pytest
from ccf_api_models.models import (
    CcfServiceReference,
    CommandItem,
    ConnectorStatus,
    ContainerProperties,
    CustomConnectorDetail,
    DeploymentDestination,
    DestinationMode,
    DocumentSummary,
    GetCustomConnectorJobResponseContent,
    GetCustomConnectorResponseContent,
    JobDetail,
    JobStatus,
    ResourceRequirements,
    SourceAccessConfiguration,
)
from custom_connector_framework import CcfClient
from custom_connector_framework import launcher as launcher_module
from pydantic import BaseModel, SecretStr

_TENANT_ARN_PREFIX = "arn:aws:ccf:us-east-1:111122223333"
_RUNTIME_ENVIRONMENT = {
    "CUSTOM_CONNECTOR_FRAMEWORK_API_ENDPOINT": "https://ccf.example.test",
    "CUSTOM_CONNECTOR_FRAMEWORK_CUSTOM_CONNECTOR_ID": "sample",
    "CUSTOM_CONNECTOR_FRAMEWORK_CUSTOM_CONNECTOR_ARN_PREFIX": _TENANT_ARN_PREFIX,
    "CUSTOM_CONNECTOR_JOB_ID": "job-1",
    "AWS_REGION": "us-east-1",
}
_TEMPORARY_CREDENTIALS = {
    "aws_access_key_id": "temporary-access-key",
    "aws_secret_access_key": "temporary-secret-key",
    "aws_session_token": "temporary-session-token",
    "region_name": "us-east-1",
}


class FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.put_count = 0
        self.delete_count = 0

    def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> None:  # noqa: N803
        assert Bucket == "destination-bucket"
        self.put_count += 1
        self.objects[Key] = Body

    def delete_object(self, *, Bucket: str, Key: str) -> None:  # noqa: N803
        assert Bucket == "destination-bucket"
        self.delete_count += 1
        self.objects.pop(Key, None)


class FakeStsClient:
    def __init__(self) -> None:
        self.requests: list[dict[str, str]] = []

    def assume_role(self, **request: str) -> dict[str, dict[str, str]]:
        assert request == {
            "RoleArn": "arn:aws:iam::444455556666:role/SampleSourceRole",
            "ExternalId": "sample-external-id",
            "RoleSessionName": "ccf-connector",
        }
        self.requests.append(request)
        return {
            "Credentials": {
                "AccessKeyId": _TEMPORARY_CREDENTIALS["aws_access_key_id"],
                "SecretAccessKey": _TEMPORARY_CREDENTIALS["aws_secret_access_key"],
                "SessionToken": _TEMPORARY_CREDENTIALS["aws_session_token"],
            }
        }


class FakeControlSession:
    def __init__(self) -> None:
        self.sts = FakeStsClient()
        self.s3 = FakeS3Client()

    def client(self, service_name: str) -> Any:
        if service_name == "sts":
            return self.sts
        if service_name == "s3":
            return self.s3
        raise AssertionError(f"Unexpected service client: {service_name}")


class FakeCcfClient(CcfClient):
    def __init__(self, mode: DestinationMode) -> None:
        self.mode = mode
        self.connector_configuration_json = "{}"
        self.state: dict[str, DocumentSummary] = {}
        self.list_calls = 0
        self.put_batches = 0
        self.delete_batches = 0

    def get_custom_connector(self, request: Any) -> GetCustomConnectorResponseContent:
        assert request.connector_id == "sample"
        return GetCustomConnectorResponseContent(
            connector=CustomConnectorDetail(
                connector_id="sample",
                arn="arn:aws:ccf:us-east-1:111122223333:connector/sample",
                name="sample",
                created_at=1.0,
                updated_at=1.0,
                status=ConnectorStatus.IN_USE,
                container_properties=ContainerProperties(
                    image_uri="111122223333.dkr.ecr.us-east-1.amazonaws.com/sample:local",
                    command=[CommandItem(root="ccf-connector"), CommandItem(root="run")],
                    resource_requirements=ResourceRequirements(cpu=1, memory=512),
                    timeout=60,
                ),
                connector_configuration=SecretStr(self.connector_configuration_json),
                source_access=SourceAccessConfiguration(
                    role_arn="arn:aws:iam::444455556666:role/SampleSourceRole",
                    external_id="sample-external-id",
                ),
            ),
            service_reference=CcfServiceReference(
                endpoint="https://ccf.example.test",
                region="us-east-1",
                service_principal="ccf.example.test",
                service_account_id="111122223333",
                destination=DeploymentDestination(
                    mode=self.mode,
                    bucket_arn="arn:aws:s3:::destination-bucket",
                    object_prefix="samples",
                ),
            ),
        )

    def get_custom_connector_job(self, request: Any) -> GetCustomConnectorJobResponseContent:
        assert request.connector_id == "sample"
        assert request.job_id == "job-1"
        return GetCustomConnectorJobResponseContent(
            job=JobDetail(
                job_id="job-1",
                connector_id="sample",
                status=JobStatus.RUNNING,
                job_configuration=SecretStr("{}"),
                created_at=1.0,
                updated_at=1.0,
            )
        )

    def list_custom_connector_documents(self, request: Any) -> Any:
        assert request.connector_id == "sample"
        self.list_calls += 1
        return type("DocumentPage", (), {"documents": list(self.state.values()), "next_token": None})()

    def batch_put_custom_connector_documents(self, request: Any) -> None:
        self.put_batches += 1
        for document in request.documents:
            self.state[document.document_id] = DocumentSummary(
                document_id=document.document_id,
                checksum=document.checksum,
                object_key=document.object_key,
                sidecar_key=document.sidecar_key,
                created_at=1.0,
                updated_at=1.0,
            )

    def batch_delete_custom_connector_documents(self, request: Any) -> None:
        self.delete_batches += 1
        for document_id in request.document_ids:
            del self.state[document_id.root]


class RuntimeHarness:
    """Run the real launcher entry point with persistent local boundaries."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mode: DestinationMode,
        source_session: Any,
    ) -> None:
        self.client = FakeCcfClient(mode)
        self.control_session = FakeControlSession()
        self.source_session = source_session
        for name, value in _RUNTIME_ENVIRONMENT.items():
            monkeypatch.setenv(name, value)
        monkeypatch.setattr(launcher_module, "_create_runtime_client", self._create_client)
        monkeypatch.setattr(launcher_module.boto3, "Session", self._create_session)

    def run(self, definition_target: str, configuration: BaseModel) -> int:
        return self.run_configuration_json(definition_target, configuration.model_dump_json())

    def run_configuration_json(self, definition_target: str, configuration_json: str) -> int:
        self.client.connector_configuration_json = configuration_json
        return launcher_module.main(["run", definition_target])

    def _create_client(self, **request: Any) -> FakeCcfClient:
        assert request == {
            "endpoint": "https://ccf.example.test",
            "region": "us-east-1",
            "session": self.control_session,
            "tenant_arn_prefix": _TENANT_ARN_PREFIX,
        }
        return self.client

    def _create_session(self, **request: str) -> Any:
        if request == {"region_name": "us-east-1"}:
            return self.control_session
        if request == _TEMPORARY_CREDENTIALS:
            return self.source_session
        raise AssertionError(f"Unexpected boto3 Session request: {request}")
