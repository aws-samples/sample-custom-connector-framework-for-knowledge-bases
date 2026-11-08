"""`CcfClient.call` takes every catalog operation's request and returns that operation's response model."""

from __future__ import annotations

import json
from typing import Any, assert_type

import boto3
import pytest
from ccf_api_models.base import CcfModel
from ccf_api_models.catalog import OperationCatalog, OperationSpec
from ccf_api_models.models import (
    BatchDeleteCustomConnectorCollectionsRequest,
    BatchDeleteCustomConnectorDocumentsRequest,
    BatchPutCustomConnectorDocumentsRequest,
    CollectionReference,
    ContainerProperties,
    CreateCustomConnectorRequest,
    CreateDestinationRequest,
    DeleteCustomConnectorCheckpointRequest,
    DeleteCustomConnectorRequest,
    DeleteDestinationRequest,
    DestinationType,
    DocumentRecord,
    GetCustomConnectorCheckpointRequest,
    GetCustomConnectorJobRequest,
    GetCustomConnectorRequest,
    GetDestinationRequest,
    GetDestinationResponseContent,
    ListCustomConnectorCollectionsRequest,
    ListCustomConnectorDocumentsRequest,
    ListCustomConnectorJobsRequest,
    ListCustomConnectorsRequest,
    ListDestinationsRequest,
    PutCustomConnectorCheckpointRequest,
    ResourceRequirements,
    SourceAccessInput,
    StartCustomConnectorJobRequest,
    StopCustomConnectorJobRequest,
    UpdateCustomConnectorRequest,
    UpdateDestinationRequest,
)
from ccf_api_models.operations import CcfApi, Operation
from pydantic import SecretStr

from custom_connector_framework.client import CcfClient, CcfClientSettings
from custom_connector_framework.client.transport import OutgoingCall

CATALOG = OperationCatalog.load()
DST = "dst-7c41e9a2b5d0"
CC = "cc-e020d65d76c0"
JOB = "ccj-0fdef5d64630"
PREFIX = "arn:aws:ccf:us-east-1:111122223333"
DESTINATION_ARN = f"{PREFIX}:destination/{DST}"
CONNECTOR_ARN = f"{DESTINATION_ARN}/custom-connector/{CC}"
TIMES = {"created_at": 1.0, "updated_at": 1.0}
CONNECTOR = {"connector_id": CC, "arn": CONNECTOR_ARN, "destination_id": DST, "name": "github", **TIMES}
DESTINATION = {
    "destination_id": DST,
    "arn": DESTINATION_ARN,
    "name": "docs",
    "type": "S3",
    "access_control_enabled": False,
    **TIMES,
}
JOB_SUMMARY = {"job_id": JOB, "arn": f"{CONNECTOR_ARN}/job/{JOB}", "status": "STARTED", **TIMES}
CONTAINER = ContainerProperties(
    image_uri="111122223333.dkr.ecr.us-east-1.amazonaws.com/ccf-connectors/github:1.0.0",
    command=["ccf-connector"],
    resource_requirements=ResourceRequirements(cpu=1.0, memory=2048),
    timeout=3600,
)
SOURCE_ROLE = "arn:aws:iam::111122223333:role/CcfSourceRole-github"
REQUESTS: dict[Operation, CcfModel] = {
    Operation.LIST_DESTINATIONS: ListDestinationsRequest(),
    Operation.CREATE_DESTINATION: CreateDestinationRequest(
        name="docs",
        type=DestinationType.S3,
        role_arn="arn:aws:iam::111122223333:role/ccf/destinations/CcfDestinationRole-docs",
    ),
    Operation.DELETE_DESTINATION: DeleteDestinationRequest(destination_id=DST),
    Operation.GET_DESTINATION: GetDestinationRequest(destination_id=DST),
    Operation.UPDATE_DESTINATION: UpdateDestinationRequest(destination_id=DST, metadata_fields=[]),
    Operation.LIST_CUSTOM_CONNECTORS: ListCustomConnectorsRequest(destination_id=DST),
    Operation.CREATE_CUSTOM_CONNECTOR: CreateCustomConnectorRequest(
        destination_id=DST,
        name="github",
        container_properties=CONTAINER,
        source_access=SourceAccessInput(role_arn=SOURCE_ROLE),
    ),
    Operation.DELETE_CUSTOM_CONNECTOR: DeleteCustomConnectorRequest(destination_id=DST, connector_id=CC),
    Operation.GET_CUSTOM_CONNECTOR: GetCustomConnectorRequest(destination_id=DST, connector_id=CC),
    Operation.UPDATE_CUSTOM_CONNECTOR: UpdateCustomConnectorRequest(destination_id=DST, connector_id=CC),
    Operation.DELETE_CUSTOM_CONNECTOR_CHECKPOINT: DeleteCustomConnectorCheckpointRequest(
        destination_id=DST, connector_id=CC
    ),
    Operation.GET_CUSTOM_CONNECTOR_CHECKPOINT: GetCustomConnectorCheckpointRequest(destination_id=DST, connector_id=CC),
    Operation.PUT_CUSTOM_CONNECTOR_CHECKPOINT: PutCustomConnectorCheckpointRequest(
        destination_id=DST, connector_id=CC, checkpoint_data=SecretStr("page-42")
    ),
    Operation.LIST_CUSTOM_CONNECTOR_COLLECTIONS: ListCustomConnectorCollectionsRequest(
        destination_id=DST, connector_id=CC
    ),
    Operation.BATCH_DELETE_CUSTOM_CONNECTOR_COLLECTIONS: BatchDeleteCustomConnectorCollectionsRequest(
        destination_id=DST, connector_id=CC, collections=[CollectionReference(collection_id="col-3f9a1c2b5d7e9f01")]
    ),
    Operation.LIST_CUSTOM_CONNECTOR_DOCUMENTS: ListCustomConnectorDocumentsRequest(destination_id=DST, connector_id=CC),
    Operation.BATCH_DELETE_CUSTOM_CONNECTOR_DOCUMENTS: BatchDeleteCustomConnectorDocumentsRequest(
        destination_id=DST, connector_id=CC, document_ids=["doc-8d41e07a9c3f2b6e"]
    ),
    Operation.BATCH_PUT_CUSTOM_CONNECTOR_DOCUMENTS: BatchPutCustomConnectorDocumentsRequest(
        destination_id=DST,
        connector_id=CC,
        documents=[
            DocumentRecord(document_id="doc-8d41e07a9c3f2b6e", source_document_id="a.md", content_hash="sha256:a")
        ],
    ),
    Operation.LIST_CUSTOM_CONNECTOR_JOBS: ListCustomConnectorJobsRequest(destination_id=DST, connector_id=CC),
    Operation.START_CUSTOM_CONNECTOR_JOB: StartCustomConnectorJobRequest(destination_id=DST, connector_id=CC),
    Operation.GET_CUSTOM_CONNECTOR_JOB: GetCustomConnectorJobRequest(destination_id=DST, connector_id=CC, job_id=JOB),
    Operation.STOP_CUSTOM_CONNECTOR_JOB: StopCustomConnectorJobRequest(destination_id=DST, connector_id=CC, job_id=JOB),
}
RESPONSES: dict[Operation, dict[str, Any]] = {
    Operation.LIST_DESTINATIONS: {"destinations": []},
    Operation.CREATE_DESTINATION: {"destination": {**DESTINATION, "status": "CREATING_BUCKET"}},
    Operation.GET_DESTINATION: {
        "destination": {
            **DESTINATION,
            "status": "ACTIVE",
            "role_arn": "arn:aws:iam::111122223333:role/ccf/destinations/CcfDestinationRole-docs",
            "destination_resources": {},
        }
    },
    Operation.UPDATE_DESTINATION: {
        "destination": {
            **DESTINATION,
            "status": "ACTIVE",
            "role_arn": "arn:aws:iam::111122223333:role/ccf/destinations/CcfDestinationRole-docs",
            "destination_resources": {},
            "metadata_fields": [],
        }
    },
    Operation.LIST_CUSTOM_CONNECTORS: {"connectors": []},
    Operation.CREATE_CUSTOM_CONNECTOR: {
        "connector": {
            **CONNECTOR,
            "status": "AVAILABLE",
            "runtime_role_arn": (
                "arn:aws:iam::444455556666:role/ccf-runtime/CcfConnectorRuntime-3c9e1f0a2b7d4e6f8a1b2c3d4e5f6a7b"
            ),
            "source_access": {"role_arn": SOURCE_ROLE, "external_id": "ccf-x"},
        }
    },
    Operation.GET_CUSTOM_CONNECTOR: {
        "connector": {
            **CONNECTOR,
            "status": "AVAILABLE",
            "runtime_role_arn": (
                "arn:aws:iam::444455556666:role/ccf-runtime/CcfConnectorRuntime-3c9e1f0a2b7d4e6f8a1b2c3d4e5f6a7b"
            ),
            "container_properties": json.loads(CONTAINER.model_dump_json()),
            "source_access": {"role_arn": SOURCE_ROLE, "external_id": "ccf-x"},
        }
    },
    Operation.UPDATE_CUSTOM_CONNECTOR: {"connector": {**CONNECTOR, "status": "AVAILABLE"}},
    Operation.GET_CUSTOM_CONNECTOR_CHECKPOINT: {"checkpoint": {"checkpoint_data": "page-42", **TIMES}},
    Operation.LIST_CUSTOM_CONNECTOR_COLLECTIONS: {"collections": []},
    Operation.LIST_CUSTOM_CONNECTOR_DOCUMENTS: {"documents": []},
    Operation.LIST_CUSTOM_CONNECTOR_JOBS: {"jobs": []},
    Operation.START_CUSTOM_CONNECTOR_JOB: {"job": JOB_SUMMARY},
    Operation.GET_CUSTOM_CONNECTOR_JOB: {"job": JOB_SUMMARY},
}


class AnsweringTransport:
    """Answers each operation with its example response and records the calls."""

    def __init__(self) -> None:
        self.calls: list[OutgoingCall] = []

    def send(self, call: OutgoingCall) -> bytes:
        self.calls.append(call)
        body = RESPONSES.get(call.spec.operation)
        return b"" if body is None else json.dumps(body).encode()


def client_over(transport: AnsweringTransport) -> CcfClient:
    settings = CcfClientSettings(
        endpoint="https://abc123.execute-api.us-east-1.amazonaws.com/ccf",
        region="us-east-1",
        session=boto3.Session(region_name="us-east-1"),
    )
    return CcfClient(CATALOG, transport, settings)  # type: ignore[arg-type]


def test_every_catalog_operation_has_an_example_request() -> None:
    assert set(REQUESTS) == {spec.operation for spec in CATALOG.specs()}


@pytest.mark.parametrize("spec", CATALOG.specs(), ids=[spec.operation.value for spec in CATALOG.specs()])
def test_call_sends_the_requests_operation_and_returns_its_response_model(spec: OperationSpec) -> None:
    transport = AnsweringTransport()

    response = client_over(transport).call(REQUESTS[spec.operation])

    assert [call.spec.operation for call in transport.calls] == [spec.operation]
    if spec.response_model is None:
        assert response is None
    else:
        assert type(response) is spec.response_model


def test_the_overloads_type_each_response() -> None:
    api: CcfApi = client_over(AnsweringTransport())

    assert_type(api.call(GetDestinationRequest(destination_id=DST)), GetDestinationResponseContent)
    assert_type(api.call(DeleteDestinationRequest(destination_id=DST)), None)
