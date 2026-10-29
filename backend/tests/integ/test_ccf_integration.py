"""Integration test for the Custom Connector Framework API.

Exercises the API end to end against a deployed stack. The API is SigV4 (IAM)
authorized, so each request is signed with the caller's AWS credentials and sent
over HTTPS. There is no boto3 ``ccf`` client; the test speaks the REST API
directly, the same way the toolkit's API client does.

Run against a deployed stack:
    CCF_ENDPOINT=https://<api-id>.execute-api.<region>.amazonaws.com/prod \
        pytest tests/integ -v
"""

import hashlib
import json
import logging
import os
import time
import uuid
from http import HTTPStatus
from typing import Any

import boto3
import pytest
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.httpsession import URLLib3Session

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

_SIGNING_SERVICE = "execute-api"
_BASE_PATH = "/api/v1/custom-connectors"
_CONNECTOR_ID_LENGTH = 15
_JOB_ID_LENGTH = 16
_MAX_PAGE = 50


class CcfHttpClient:
    """Signs and sends REST requests to the deployed CCF API."""

    def __init__(self, endpoint: str, region: str) -> None:
        self._endpoint = endpoint.rstrip("/")
        credentials = boto3.Session().get_credentials()
        if credentials is None:
            raise ValueError("No AWS credentials available to sign CCF API requests.")
        self._signer = SigV4Auth(credentials.get_frozen_credentials(), _SIGNING_SERVICE, region)
        self._session = URLLib3Session()

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        data = json.dumps(body).encode("utf-8") if body is not None else b""
        headers = {"Content-Type": "application/json"} if body is not None else {}
        request = AWSRequest(method=method, url=f"{self._endpoint}{path}", data=data, headers=headers)
        self._signer.add_auth(request)
        response = self._session.send(request.prepare())
        decoded = json.loads(response.content) if response.content else {}
        return response.status_code, decoded


def _resolve_endpoint() -> str:
    endpoint = os.environ.get("CCF_ENDPOINT")
    if endpoint:
        return endpoint
    cloudformation = boto3.client("cloudformation")
    stacks = cloudformation.describe_stacks(StackName="CcfDevStack")["Stacks"]
    for output in stacks[0].get("Outputs", []):
        if output["OutputKey"].endswith("ApiEndpoint"):
            return output["OutputValue"]
    raise ValueError("Set CCF_ENDPOINT or deploy a stack that outputs ApiEndpoint.")


class CcfIntegrationContext:
    """Shared state across the ordered integration steps."""

    def __init__(self) -> None:
        region = boto3.session.Session().region_name or "us-east-1"
        self.client = CcfHttpClient(_resolve_endpoint(), region)
        account_id = boto3.client("sts").get_caller_identity()["Account"]
        self.image_uri = f"{account_id}.dkr.ecr.{region}.amazonaws.com/fake-hello-world:latest"
        self.execution_role_arn = f"arn:aws:iam::{account_id}:role/fake-execution-role"
        self.job_role_arn = f"arn:aws:iam::{account_id}:role/fake-job-role"
        self.connector_id: str | None = None
        self.job_id: str | None = None
        self.document_ids: list[str] = []

    def cleanup(self) -> None:
        if self.connector_id:
            self.client.request("DELETE", f"{_BASE_PATH}/{self.connector_id}")


@pytest.fixture(scope="module")
def ccf() -> CcfIntegrationContext:
    context = CcfIntegrationContext()
    yield context
    context.cleanup()


def test_01_create_connector(ccf: CcfIntegrationContext) -> None:
    name = f"test-connector-{uuid.uuid4().hex[:8]}"
    status, body = ccf.client.request(
        "POST",
        _BASE_PATH,
        {
            "name": name,
            "description": "Integration test connector",
            "container_properties": {
                "image_uri": ccf.image_uri,
                "execution_role_arn": ccf.execution_role_arn,
                "job_role_arn": ccf.job_role_arn,
                "resource_requirements": {"cpu": 1, "memory": 2048},
                "timeout": 3600,
            },
        },
    )
    assert status == HTTPStatus.CREATED
    ccf.connector_id = body["connector"]["connector_id"]
    assert ccf.connector_id.startswith("cc-")
    assert len(ccf.connector_id) == _CONNECTOR_ID_LENGTH
    assert body["connector"]["name"] == name


def test_02_list_connectors(ccf: CcfIntegrationContext) -> None:
    status, body = ccf.client.request("GET", _BASE_PATH)
    assert status == HTTPStatus.OK
    assert len(body["connectors"]) <= _MAX_PAGE
    assert any(c["connector_id"] == ccf.connector_id for c in body["connectors"])


def test_03_get_connector(ccf: CcfIntegrationContext) -> None:
    status, body = ccf.client.request("GET", f"{_BASE_PATH}/{ccf.connector_id}")
    assert status == HTTPStatus.OK
    assert body["connector"]["connector_id"] == ccf.connector_id
    assert "container_properties" in body["connector"]


def test_04_update_connector(ccf: CcfIntegrationContext) -> None:
    _, current = ccf.client.request("GET", f"{_BASE_PATH}/{ccf.connector_id}")
    updated_description = f"{current['connector']['description']} - updated"
    status, body = ccf.client.request(
        "PUT",
        f"{_BASE_PATH}/{ccf.connector_id}",
        {"name": current["connector"]["name"], "description": updated_description},
    )
    assert status == HTTPStatus.OK
    assert body["connector"]["description"] == updated_description


def test_05_put_checkpoint(ccf: CcfIntegrationContext) -> None:
    status, _ = ccf.client.request(
        "PUT",
        f"{_BASE_PATH}/{ccf.connector_id}/checkpoint",
        {"checkpoint_data": json.dumps({"last_sync_time": "2026-01-01T00:00:00Z"})},
    )
    assert status == HTTPStatus.ACCEPTED


def test_06_get_checkpoint(ccf: CcfIntegrationContext) -> None:
    status, body = ccf.client.request("GET", f"{_BASE_PATH}/{ccf.connector_id}/checkpoint")
    assert status == HTTPStatus.OK
    assert isinstance(body["checkpoint"]["checkpoint_data"], str)


def test_07_start_job(ccf: CcfIntegrationContext) -> None:
    status, body = ccf.client.request(
        "POST",
        f"{_BASE_PATH}/{ccf.connector_id}/jobs",
        {"environment": [{"name": "TEST_VAR", "value": "test_value"}]},
    )
    assert status == HTTPStatus.CREATED
    ccf.job_id = body["job"]["job_id"]
    assert ccf.job_id.startswith("ccj-")
    assert len(ccf.job_id) == _JOB_ID_LENGTH


def test_08_list_jobs(ccf: CcfIntegrationContext) -> None:
    status, body = ccf.client.request("GET", f"{_BASE_PATH}/{ccf.connector_id}/jobs")
    assert status == HTTPStatus.OK
    assert len(body["jobs"]) <= _MAX_PAGE
    assert any(job["job_id"] == ccf.job_id for job in body["jobs"])


def test_10_put_documents(ccf: CcfIntegrationContext) -> None:
    documents = [
        {"document_id": f"test-doc-{uuid.uuid4().hex}", "checksum": hashlib.sha256(content.encode()).hexdigest()}
        for content in ("test document 1", "test document 2")
    ]
    status, _ = ccf.client.request("POST", f"{_BASE_PATH}/{ccf.connector_id}/documents", {"documents": documents})
    assert status == HTTPStatus.ACCEPTED
    ccf.document_ids = [doc["document_id"] for doc in documents]


def test_11_list_documents(ccf: CcfIntegrationContext) -> None:
    status, body = ccf.client.request("GET", f"{_BASE_PATH}/{ccf.connector_id}/documents")
    assert status == HTTPStatus.OK
    assert len(body["documents"]) <= _MAX_PAGE


def test_12_delete_documents(ccf: CcfIntegrationContext) -> None:
    status, _ = ccf.client.request(
        "DELETE",
        f"{_BASE_PATH}/{ccf.connector_id}/documents",
        {"document_ids": ccf.document_ids},
    )
    assert status == HTTPStatus.ACCEPTED
    _, body = ccf.client.request("GET", f"{_BASE_PATH}/{ccf.connector_id}/documents")
    listed = {doc["document_id"] for doc in body["documents"]}
    assert listed.isdisjoint(ccf.document_ids)


def test_13_delete_checkpoint(ccf: CcfIntegrationContext) -> None:
    status, _ = ccf.client.request("DELETE", f"{_BASE_PATH}/{ccf.connector_id}/checkpoint")
    assert status == HTTPStatus.ACCEPTED


def test_14_delete_connector(ccf: CcfIntegrationContext) -> None:
    for _ in range(10):
        _, body = ccf.client.request("GET", f"{_BASE_PATH}/{ccf.connector_id}")
        if body["connector"]["status"] == "AVAILABLE":
            break
        time.sleep(5)
    status, _ = ccf.client.request("DELETE", f"{_BASE_PATH}/{ccf.connector_id}")
    assert status == HTTPStatus.NO_CONTENT
    ccf.connector_id = None
