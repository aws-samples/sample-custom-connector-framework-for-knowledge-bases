"""PRIVATE generated CCF client operations. DO NOT EDIT."""
# ruff: noqa: E501

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from ccf_api_models.base import CcfModel
from ccf_api_models.models import (
    BatchDeleteCustomConnectorDocumentsRequest,
    BatchPutCustomConnectorDocumentsRequest,
    CreateCustomConnectorRequest,
    CreateCustomConnectorResponseContent,
    DeleteCustomConnectorCheckpointRequest,
    DeleteCustomConnectorRequest,
    GetCustomConnectorCheckpointRequest,
    GetCustomConnectorCheckpointResponseContent,
    GetCustomConnectorJobRequest,
    GetCustomConnectorJobResponseContent,
    GetCustomConnectorRequest,
    GetCustomConnectorResponseContent,
    ListCustomConnectorDocumentsRequest,
    ListCustomConnectorDocumentsResponseContent,
    ListCustomConnectorJobsRequest,
    ListCustomConnectorJobsResponseContent,
    ListCustomConnectorsRequest,
    ListCustomConnectorsResponseContent,
    PutCustomConnectorCheckpointRequest,
    StartCustomConnectorJobRequest,
    StartCustomConnectorJobResponseContent,
    StopCustomConnectorJobRequest,
    UpdateCustomConnectorRequest,
    UpdateCustomConnectorResponseContent,
)


@dataclass(frozen=True)
class _Operation:
    """Generated HTTP bindings consumed by the private transport."""

    python_name: str
    method: str
    path: str
    success_status: int
    request_model: type[CcfModel]
    response_model: type[CcfModel] | None
    path_bindings: tuple[tuple[str, str], ...]
    query_bindings: tuple[tuple[str, str], ...]
    body_members: tuple[str, ...]


GENERATED_OPERATIONS: tuple[_Operation, ...] = (
    _Operation(
        'batch_delete_custom_connector_documents', 'DELETE', '/api/v1/custom-connectors/{connector_id}/documents', 202,
        BatchDeleteCustomConnectorDocumentsRequest, None, (('connector_id', 'connector_id'),), (), ('document_ids',),
    ),
    _Operation(
        'batch_put_custom_connector_documents', 'POST', '/api/v1/custom-connectors/{connector_id}/documents', 202,
        BatchPutCustomConnectorDocumentsRequest, None, (('connector_id', 'connector_id'),), (), ('documents',),
    ),
    _Operation(
        'create_custom_connector', 'POST', '/api/v1/custom-connectors', 201,
        CreateCustomConnectorRequest, CreateCustomConnectorResponseContent, (), (), ('connector_configuration', 'container_properties', 'description', 'name', 'schedule', 'source_access'),
    ),
    _Operation(
        'delete_custom_connector', 'DELETE', '/api/v1/custom-connectors/{connector_id}', 204,
        DeleteCustomConnectorRequest, None, (('connector_id', 'connector_id'),), (), (),
    ),
    _Operation(
        'delete_custom_connector_checkpoint', 'DELETE', '/api/v1/custom-connectors/{connector_id}/checkpoint', 202,
        DeleteCustomConnectorCheckpointRequest, None, (('connector_id', 'connector_id'),), (), (),
    ),
    _Operation(
        'get_custom_connector', 'GET', '/api/v1/custom-connectors/{connector_id}', 200,
        GetCustomConnectorRequest, GetCustomConnectorResponseContent, (('connector_id', 'connector_id'),), (), (),
    ),
    _Operation(
        'get_custom_connector_checkpoint', 'GET', '/api/v1/custom-connectors/{connector_id}/checkpoint', 200,
        GetCustomConnectorCheckpointRequest, GetCustomConnectorCheckpointResponseContent, (('connector_id', 'connector_id'),), (), (),
    ),
    _Operation(
        'get_custom_connector_job', 'GET', '/api/v1/custom-connectors/{connector_id}/jobs/{job_id}', 200,
        GetCustomConnectorJobRequest, GetCustomConnectorJobResponseContent, (('connector_id', 'connector_id'), ('job_id', 'job_id')), (), (),
    ),
    _Operation(
        'list_custom_connector_documents', 'GET', '/api/v1/custom-connectors/{connector_id}/documents', 200,
        ListCustomConnectorDocumentsRequest, ListCustomConnectorDocumentsResponseContent, (('connector_id', 'connector_id'),), (('max_results', 'max_results'), ('next_token', 'next_token')), (),
    ),
    _Operation(
        'list_custom_connector_jobs', 'GET', '/api/v1/custom-connectors/{connector_id}/jobs', 200,
        ListCustomConnectorJobsRequest, ListCustomConnectorJobsResponseContent, (('connector_id', 'connector_id'),), (('max_results', 'max_results'), ('next_token', 'next_token'), ('status', 'status')), (),
    ),
    _Operation(
        'list_custom_connectors', 'GET', '/api/v1/custom-connectors', 200,
        ListCustomConnectorsRequest, ListCustomConnectorsResponseContent, (), (('max_results', 'max_results'), ('next_token', 'next_token')), (),
    ),
    _Operation(
        'put_custom_connector_checkpoint', 'PUT', '/api/v1/custom-connectors/{connector_id}/checkpoint', 202,
        PutCustomConnectorCheckpointRequest, None, (('connector_id', 'connector_id'),), (), ('checkpoint_data',),
    ),
    _Operation(
        'start_custom_connector_job', 'POST', '/api/v1/custom-connectors/{connector_id}/jobs', 201,
        StartCustomConnectorJobRequest, StartCustomConnectorJobResponseContent, (('connector_id', 'connector_id'),), (), ('job_configuration',),
    ),
    _Operation(
        'stop_custom_connector_job', 'POST', '/api/v1/custom-connectors/{connector_id}/jobs/{job_id}/stop', 202,
        StopCustomConnectorJobRequest, None, (('connector_id', 'connector_id'), ('job_id', 'job_id')), (), (),
    ),
    _Operation(
        'update_custom_connector', 'PUT', '/api/v1/custom-connectors/{connector_id}', 200,
        UpdateCustomConnectorRequest, UpdateCustomConnectorResponseContent, (('connector_id', 'connector_id'),), (), ('connector_configuration', 'container_properties', 'description', 'name', 'schedule', 'source_access'),
    ),
)


class _GeneratedCcfOperations:
    """Generated typed operation surface mixed into the sole public client."""

    def _invoke(self, operation: _Operation, request: CcfModel) -> CcfModel | None:
        raise NotImplementedError

    def batch_delete_custom_connector_documents(self, request: BatchDeleteCustomConnectorDocumentsRequest) -> None:
        self._invoke(GENERATED_OPERATIONS[0], request)

    def batch_put_custom_connector_documents(self, request: BatchPutCustomConnectorDocumentsRequest) -> None:
        self._invoke(GENERATED_OPERATIONS[1], request)

    def create_custom_connector(self, request: CreateCustomConnectorRequest) -> CreateCustomConnectorResponseContent:
        return cast(CreateCustomConnectorResponseContent, self._invoke(GENERATED_OPERATIONS[2], request))

    def delete_custom_connector(self, request: DeleteCustomConnectorRequest) -> None:
        self._invoke(GENERATED_OPERATIONS[3], request)

    def delete_custom_connector_checkpoint(self, request: DeleteCustomConnectorCheckpointRequest) -> None:
        self._invoke(GENERATED_OPERATIONS[4], request)

    def get_custom_connector(self, request: GetCustomConnectorRequest) -> GetCustomConnectorResponseContent:
        return cast(GetCustomConnectorResponseContent, self._invoke(GENERATED_OPERATIONS[5], request))

    def get_custom_connector_checkpoint(self, request: GetCustomConnectorCheckpointRequest) -> GetCustomConnectorCheckpointResponseContent:
        return cast(GetCustomConnectorCheckpointResponseContent, self._invoke(GENERATED_OPERATIONS[6], request))

    def get_custom_connector_job(self, request: GetCustomConnectorJobRequest) -> GetCustomConnectorJobResponseContent:
        return cast(GetCustomConnectorJobResponseContent, self._invoke(GENERATED_OPERATIONS[7], request))

    def list_custom_connector_documents(self, request: ListCustomConnectorDocumentsRequest) -> ListCustomConnectorDocumentsResponseContent:
        return cast(ListCustomConnectorDocumentsResponseContent, self._invoke(GENERATED_OPERATIONS[8], request))

    def list_custom_connector_jobs(self, request: ListCustomConnectorJobsRequest) -> ListCustomConnectorJobsResponseContent:
        return cast(ListCustomConnectorJobsResponseContent, self._invoke(GENERATED_OPERATIONS[9], request))

    def list_custom_connectors(self, request: ListCustomConnectorsRequest) -> ListCustomConnectorsResponseContent:
        return cast(ListCustomConnectorsResponseContent, self._invoke(GENERATED_OPERATIONS[10], request))

    def put_custom_connector_checkpoint(self, request: PutCustomConnectorCheckpointRequest) -> None:
        self._invoke(GENERATED_OPERATIONS[11], request)

    def start_custom_connector_job(self, request: StartCustomConnectorJobRequest) -> StartCustomConnectorJobResponseContent:
        return cast(StartCustomConnectorJobResponseContent, self._invoke(GENERATED_OPERATIONS[12], request))

    def stop_custom_connector_job(self, request: StopCustomConnectorJobRequest) -> None:
        self._invoke(GENERATED_OPERATIONS[13], request)

    def update_custom_connector(self, request: UpdateCustomConnectorRequest) -> UpdateCustomConnectorResponseContent:
        return cast(UpdateCustomConnectorResponseContent, self._invoke(GENERATED_OPERATIONS[14], request))


__all__: list[str] = []
