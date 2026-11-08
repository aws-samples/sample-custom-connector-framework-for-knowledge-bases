"""The destination create, update, and delete activities; Get and List are the platform's `ReadResource` and
`ListResources`.

Creating a destination saves it in `CREATING_BUCKET` with a new random ID and starts the
destination workflow, which records its own execution and builds the bucket (and, for `BEDROCK`,
the knowledge base and data source) through the tenant's destination role. Updating one sets its
metadata fields on the knowledge base through the same role, then stores them. Deleting one sets
`DELETING` first, so no custom connector can be created while the delete checks that none remains,
and then starts the delete path.
"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar, Literal

from ccf_api_models.identifiers import DestinationRef, GeneratedIdKind
from ccf_api_models.models import (
    CreateDestinationRequest,
    CreateDestinationResponseContent,
    DeleteDestinationRequest,
    DestinationStatus,
    DestinationType,
    UpdateDestinationRequest,
    UpdateDestinationResponseContent,
)
from pydantic import BaseModel, ConfigDict

from common.errors import (
    BadRequestError,
    DependencyThrottledError,
    DependencyUnavailableError,
    ResourceConflictError,
)
from common.observability import logger
from common.operation import OperationRequest, RequestTrace
from common.resource_ids import ResourceIds
from common.storage.bedrock.knowledge_base_fields_dao import KnowledgeBaseFieldsDao, SetMetadataFieldsRequest
from common.storage.ddb.custom_connectors_dao import CustomConnectorsDao
from common.storage.ddb.destinations_dao import (
    CreateDestinationRecord,
    DestinationsDao,
    MetadataFieldsChange,
    StoredDestination,
)
from common.validators.create_destination_validator import CreateDestinationValidator
from common.workflow_executions import ExistingExecution, WorkflowExecutions

_REQUEST_ID_CHARACTERS = 8
"""How many characters of the API Gateway request ID a delete's execution name keeps."""
_MILLISECONDS_PER_SECOND = 1000


class DestinationWorkflowAction(StrEnum):
    CREATE = "CREATE"
    DELETE = "DELETE"


class DestinationWorkflowInput(BaseModel):
    """The members both workflow paths read; a retried request names the execution the first call started."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    EXISTING_EXECUTION: ClassVar[ExistingExecution] = ExistingExecution.REUSE

    destination_id: str
    tenant_arn_prefix: str
    role_arn: str
    type: DestinationType
    access_control_enabled: bool
    correlation_id: str
    api_request_id: str
    execution_name: str
    """The workflow names a continued execution from it."""
    continuation: int = 0
    """The API always sends 0; a delete that continues in a new execution sends one higher."""

    @classmethod
    def of(cls, request: _WorkflowStart) -> DestinationWorkflowInput:
        stored, trace = request.stored, request.trace
        return cls(
            destination_id=stored.destination_id,
            tenant_arn_prefix=stored.tenant_arn_prefix,
            role_arn=stored.role_arn,
            type=stored.type,
            access_control_enabled=stored.access_control_enabled,
            correlation_id=trace.correlation_id,
            api_request_id=trace.api_request_id,
            execution_name=request.execution_name,
        )


class CreateDestinationWorkflowInput(DestinationWorkflowInput):
    action: Literal[DestinationWorkflowAction.CREATE] = DestinationWorkflowAction.CREATE
    boundary_arn: str
    """The destination role's permissions boundary, which the create path puts on the knowledge base role."""


class DeleteDestinationWorkflowInput(DestinationWorkflowInput):
    action: Literal[DestinationWorkflowAction.DELETE] = DestinationWorkflowAction.DELETE


class _WorkflowStart(BaseModel):
    model_config = ConfigDict(frozen=True)

    stored: StoredDestination
    trace: RequestTrace
    execution_name: str


class CreateDestinationActivity:
    def __init__(
        self,
        validator: CreateDestinationValidator,
        destinations: DestinationsDao,
        workflow: WorkflowExecutions,
        ids: ResourceIds,
    ) -> None:
        self._validator = validator
        self._destinations = destinations
        self._workflow = workflow
        self._ids = ids

    def create(self, request: OperationRequest[CreateDestinationRequest]) -> CreateDestinationResponseContent:
        """Return the destination, or delete it again when its workflow can't start."""
        role = self._validator.validate(request)
        destination = DestinationRef(tenant=request.tenant, destination_id=self._ids.new(GeneratedIdKind.DESTINATION))
        stored = self._destinations.create(
            CreateDestinationRecord(
                destination=destination, payload=request.payload, correlation_id=request.trace.correlation_id
            )
        )
        name = f"{stored.destination_id}-create-{int(stored.created_at * _MILLISECONDS_PER_SECOND)}"
        start = _WorkflowStart(stored=stored, trace=request.trace, execution_name=name)
        shared = DestinationWorkflowInput.of(start).model_dump()
        workflow_input = CreateDestinationWorkflowInput.model_validate({**shared, "boundary_arn": role.boundary_arn})
        try:
            self._workflow.start(workflow_input)
        except DependencyUnavailableError, DependencyThrottledError:
            self._delete_unstarted(destination)
            raise
        return CreateDestinationResponseContent(destination=stored.created())

    def _delete_unstarted(self, destination: DestinationRef) -> None:
        """Remove a destination whose workflow didn't start; a failure here is logged, not raised over the cause."""
        try:
            self._destinations.delete_unstarted(destination)
        except Exception as error:
            logger.error(
                "destination_compensation_failed",
                extra={"destination_id": destination.destination_id, "error_type": type(error).__name__},
            )


class UpdateDestinationActivity:
    """Sets a `BEDROCK` destination's metadata fields on its knowledge base, then stores them. Bedrock checks the
    fields, so a rejected list changes nothing, and the destination stays `ACTIVE` throughout."""

    def __init__(self, destinations: DestinationsDao, knowledge_bases: KnowledgeBaseFieldsDao) -> None:
        self._destinations = destinations
        self._knowledge_bases = knowledge_bases

    def update(self, request: OperationRequest[UpdateDestinationRequest]) -> UpdateDestinationResponseContent:
        destination = DestinationRef.of(request)
        fields = tuple(request.payload.metadata_fields)
        current = self._destinations.get(destination)
        if current.type is not DestinationType.BEDROCK:
            raise BadRequestError("Only a BEDROCK destination has metadata fields", fields=("metadata_fields",))
        knowledge_base_id = current.destination_resources.knowledge_base_id
        if current.status is not DestinationStatus.ACTIVE or knowledge_base_id is None:
            raise ResourceConflictError("Destination is not ACTIVE")
        self._knowledge_bases.set_fields(
            SetMetadataFieldsRequest(
                destination_id=current.destination_id,
                role_arn=current.role_arn,
                knowledge_base_id=knowledge_base_id,
                fields=fields,
            )
        )
        change = MetadataFieldsChange(destination=destination, fields=fields)
        return UpdateDestinationResponseContent(
            destination=self._destinations.replace_metadata_fields(change).destination()
        )


class DeleteDestinationActivity:
    """Sets the destination `DELETING`, checks it holds no custom connector, and starts the delete path."""

    def __init__(
        self, destinations: DestinationsDao, connectors: CustomConnectorsDao, workflow: WorkflowExecutions
    ) -> None:
        self._destinations = destinations
        self._connectors = connectors
        self._workflow = workflow

    def delete(self, request: OperationRequest[DeleteDestinationRequest]) -> None:
        """Start the deletion, or return when a deletion execution is already running."""
        destination = DestinationRef.of(request)
        stored = self._destinations.get(destination)
        name = f"{stored.destination_id}-delete-{request.trace.api_request_id[:_REQUEST_ID_CHARACTERS]}"
        match stored.status:
            case (
                DestinationStatus.CREATING_BUCKET
                | DestinationStatus.CREATING_KNOWLEDGE_BASE
                | DestinationStatus.CREATING_DATA_SOURCE
            ):
                raise ResourceConflictError("Destination is still being created")
            case DestinationStatus.DELETING:
                execution = stored.workflow_execution_arn
                if execution is not None and self._workflow.is_running(execution):
                    return
                self._ensure_no_connectors(destination)
                self._workflow.start(
                    DeleteDestinationWorkflowInput.of(
                        _WorkflowStart(stored=stored, trace=request.trace, execution_name=name)
                    )
                )
            case DestinationStatus.ACTIVE | DestinationStatus.FAILED:
                begun = self._destinations.begin_deletion(destination)
                self._ensure_no_connectors(destination)
                start = _WorkflowStart(stored=begun, trace=request.trace, execution_name=name)
                try:
                    self._workflow.start(DeleteDestinationWorkflowInput.of(start))
                except DependencyUnavailableError, DependencyThrottledError:
                    self._destinations.restore_previous_status(destination)
                    raise

    def _ensure_no_connectors(self, destination: DestinationRef) -> None:
        """Restore the status this delete replaced and answer 409 when a custom connector remains."""
        if self._connectors.has_connectors(destination):
            self._destinations.restore_previous_status(destination)
            raise ResourceConflictError("Destination still contains a custom connector")
