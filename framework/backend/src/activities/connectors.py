"""One activity per custom connector write operation.

A create makes the runtime role, the job definition, and the schedule before it writes the item,
because the item must never name a role or definition that doesn't exist. An update changes the
role, definition, and schedule before it writes the item for the same reason. Each made change
records how to undo itself, so a failure later in the same call leaves nothing behind.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar

from ccf_api_models.identifiers import ConnectorRef, DestinationRef, GeneratedIdKind
from ccf_api_models.models import (
    Connector,
    ConnectorStatus,
    ConnectorSummary,
    CreateCustomConnectorRequest,
    CreateCustomConnectorResponseContent,
    CreatedConnector,
    DeleteCustomConnectorRequest,
    DestinationStatus,
    DestinationType,
    GetCustomConnectorRequest,
    GetCustomConnectorResponseContent,
    UpdateCustomConnectorRequest,
    UpdateCustomConnectorResponseContent,
)
from pydantic import BaseModel, ConfigDict

from common.connector_job_definitions import ConnectorJobDefinitions, RegisterJobDefinitionRequest
from common.errors import ResourceConflictError
from common.external_ids import ExternalIdGenerator
from common.observability import logger
from common.operation import OperationRequest
from common.resource_data_codec import DecryptResourceDataRequest, EncryptResourceDataRequest, ResourceDataCodec
from common.resource_ids import ResourceIds
from common.runtime_role import ConnectorRuntimeRoleLifecycle, RuntimeRoleAccess
from common.schedule_lifecycle import ScheduleLifecycle, ScheduleLifecycleRequest
from common.storage.ddb.custom_connectors_dao import (
    ConnectorChange,
    ConnectorUpdate,
    CreateConnectorRecord,
    CustomConnectorsDao,
    DeletionExecutionRecord,
)
from common.storage.ddb.destinations_dao import DestinationsDao
from common.validators.connector_request_validator import ConnectorRequestValidator
from common.workflow_executions import ExistingExecution, WorkflowExecutions

_REQUEST_ID_CHARACTERS = 8
_CONNECTOR_MEMBERS = frozenset(Connector.model_fields) - {"connector_configuration"}


class ConnectorDeletionWorkflowInput(BaseModel):
    """The execution input of `CcfConnectorDeletionWorkflow`; every field is one its states read with JSONPath."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    EXISTING_EXECUTION: ClassVar[ExistingExecution] = ExistingExecution.FAIL

    tenant_arn_prefix: str
    destination_id: str
    connector_id: str
    connector_arn: str
    destination_connector_key: str
    destination_type: DestinationType
    bucket_name: str
    destination_role_arn: str
    runtime_role_name: str
    schedule_name: str
    job_definition_arn: str | None
    """The revision the workflow deregisters; None for a custom connector created before it was stored."""
    correlation_id: str
    api_request_id: str
    continuation: int = 0
    """0 for the execution the API starts; an execution that continues in a new one passes it one higher."""

    @property
    def execution_name(self) -> str:
        """`<connector_id>-delete-<first 8 of the API request ID>`."""
        return f"{self.connector_id}-delete-{self.api_request_id[:_REQUEST_ID_CHARACTERS]}"


class _CompensationStep(BaseModel):
    """Undoes one change a failed create or update made."""

    model_config = ConfigDict(frozen=True)

    name: str
    connector_id: str
    run: Callable[[], None]

    def undo(self) -> None:
        """Run the step; a failure is logged, so the caller still re-raises the original error."""
        try:
            self.run()
        except Exception as error:
            logger.error(
                "connector_compensation_failed",
                extra={"connector_id": self.connector_id, "step": self.name, "error_type": type(error).__name__},
            )


class CreateCustomConnectorActivity:
    """Validates, creates the runtime role, job definition, and schedule, and saves the custom connector."""

    def __init__(
        self,
        connectors: CustomConnectorsDao,
        destinations: DestinationsDao,
        validator: ConnectorRequestValidator,
        codec: ResourceDataCodec,
        runtime_roles: ConnectorRuntimeRoleLifecycle,
        schedules: ScheduleLifecycle,
        job_definitions: ConnectorJobDefinitions,
        external_ids: ExternalIdGenerator,
        ids: ResourceIds,
    ) -> None:
        self._connectors = connectors
        self._destinations = destinations
        self._validator = validator
        self._codec = codec
        self._runtime_roles = runtime_roles
        self._schedules = schedules
        self._job_definitions = job_definitions
        self._external_ids = external_ids
        self._ids = ids

    def create(self, request: OperationRequest[CreateCustomConnectorRequest]) -> CreateCustomConnectorResponseContent:
        """Return the new `AVAILABLE` custom connector; on any failure, delete what this call created."""
        payload = request.payload
        self._validator.validate(request)
        destination = DestinationRef(tenant=request.tenant, destination_id=payload.destination_id)
        # This read answers 404 for a missing destination and stops IAM and Scheduler changes for one that isn't ACTIVE.
        if self._destinations.get(destination).status is not DestinationStatus.ACTIVE:
            raise ResourceConflictError("Destination is not ACTIVE")
        connector = ConnectorRef(destination=destination, connector_id=self._ids.new(GeneratedIdKind.CUSTOM_CONNECTOR))
        configuration = payload.connector_configuration
        encrypted = (
            None
            if configuration is None
            else self._codec.encrypt(EncryptResourceDataRequest(data=configuration, resource_arn=connector.arn))
        )
        log_destination = payload.log_destination
        undo: list[_CompensationStep] = []
        try:
            undo.append(
                _CompensationStep(
                    name="runtime_role_delete",
                    connector_id=connector.connector_id,
                    run=lambda: self._runtime_roles.delete(connector),
                )
            )
            runtime_role_arn = self._runtime_roles.create(
                RuntimeRoleAccess(
                    connector=connector,
                    source_role_arn=payload.source_access.role_arn,
                    log_delivery_role_arn=None if log_destination is None else log_destination.role_arn,
                )
            )
            record = CreateConnectorRecord(
                connector=connector,
                payload=payload,
                external_id=self._external_ids.generate(),
                runtime_role_arn=runtime_role_arn,
                encrypted_configuration=encrypted,
            )
            definition_arn = self._job_definitions.register(
                RegisterJobDefinitionRequest(connector=self._connectors.new_item(record))
            )
            record = record.model_copy(update={"job_definition_arn": definition_arn})
            undo.append(
                _CompensationStep(
                    name="job_definition_delete",
                    connector_id=connector.connector_id,
                    run=lambda: self._job_definitions.deregister(definition_arn),
                )
            )
            if payload.schedule is not None:
                undo.append(
                    _CompensationStep(
                        name="schedule_delete",
                        connector_id=connector.connector_id,
                        run=lambda: self._schedules.delete(connector),
                    )
                )
                self._schedules.create(ScheduleLifecycleRequest(connector=connector, schedule=payload.schedule))
            stored = self._connectors.create(record)
        except Exception:
            for step in reversed(undo):
                step.undo()
            raise
        return CreateCustomConnectorResponseContent(
            connector=CreatedConnector.model_validate(stored, from_attributes=True)
        )


class GetCustomConnectorActivity:
    """Returns one custom connector with its decrypted configuration and its external ID.

    A runtime role's call reuses the item the tenant resolver read, so it reads the table once.
    """

    def __init__(self, connectors: CustomConnectorsDao, codec: ResourceDataCodec) -> None:
        self._connectors = connectors
        self._codec = codec

    def fetch(self, request: OperationRequest[GetCustomConnectorRequest]) -> GetCustomConnectorResponseContent:
        stored = request.connector or self._connectors.get(ConnectorRef.of(request))
        ciphertext = stored.encrypted_configuration
        configuration = (
            None
            if ciphertext is None
            else self._codec.decrypt(DecryptResourceDataRequest(ciphertext=ciphertext, resource_arn=stored.arn))
        )
        return GetCustomConnectorResponseContent(
            connector=Connector.model_validate(
                {**stored.model_dump(include=set(_CONNECTOR_MEMBERS)), "connector_configuration": configuration}
            )
        )


class UpdateCustomConnectorActivity:
    """Replaces the members a request sends while the custom connector is `AVAILABLE`."""

    def __init__(
        self,
        connectors: CustomConnectorsDao,
        validator: ConnectorRequestValidator,
        codec: ResourceDataCodec,
        runtime_roles: ConnectorRuntimeRoleLifecycle,
        schedules: ScheduleLifecycle,
        job_definitions: ConnectorJobDefinitions,
    ) -> None:
        self._connectors = connectors
        self._validator = validator
        self._codec = codec
        self._runtime_roles = runtime_roles
        self._schedules = schedules
        self._job_definitions = job_definitions

    def update(self, request: OperationRequest[UpdateCustomConnectorRequest]) -> UpdateCustomConnectorResponseContent:
        """Return the updated summary; a failure puts back the stored role policy, job definition, and schedule."""
        payload = request.payload
        self._validator.validate(request)
        connector = ConnectorRef.of(request)
        stored = self._connectors.get(connector)
        if stored.status is not ConnectorStatus.AVAILABLE:
            raise ResourceConflictError("Custom connector is in use or being deleted")
        configuration = payload.connector_configuration
        encrypted = (
            None
            if configuration is None
            else self._codec.encrypt(EncryptResourceDataRequest(data=configuration, resource_arn=connector.arn))
        )
        merged = stored.with_update(ConnectorChange(payload=payload, encrypted_configuration=encrypted))
        undo: list[_CompensationStep] = []
        try:
            if merged.runtime_role_access() != stored.runtime_role_access():
                undo.append(
                    _CompensationStep(
                        name="runtime_role_restore",
                        connector_id=connector.connector_id,
                        run=lambda: self._runtime_roles.replace_policy(stored.runtime_role_access()),
                    )
                )
                self._runtime_roles.replace_policy(merged.runtime_role_access())
            # The job definition holds the container, the source access, and the log destination.
            if (merged.container_properties, merged.source_access, merged.log_destination) != (
                stored.container_properties,
                stored.source_access,
                stored.log_destination,
            ):
                definition_arn = self._job_definitions.register(RegisterJobDefinitionRequest(connector=merged))
                undo.append(
                    _CompensationStep(
                        name="job_definition_restore",
                        connector_id=connector.connector_id,
                        run=lambda: self._job_definitions.deregister(definition_arn),
                    )
                )
                merged = merged.model_copy(update={"job_definition_arn": definition_arn})
            if merged.schedule != stored.schedule:
                undo.append(
                    _CompensationStep(
                        name="schedule_restore",
                        connector_id=connector.connector_id,
                        run=lambda: self._schedules.replace(
                            ScheduleLifecycleRequest(connector=connector, schedule=stored.schedule)
                        ),
                    )
                )
                self._schedules.replace(ScheduleLifecycleRequest(connector=connector, schedule=merged.schedule))
            updated = self._connectors.update(ConnectorUpdate(stored=stored, merged=merged))
        except Exception:
            for step in reversed(undo):
                step.undo()
            raise
        previous_definition_arn = stored.job_definition_arn
        if previous_definition_arn is not None and previous_definition_arn != updated.job_definition_arn:
            # Jobs run the definition by name, so a revision left behind is never run; it is only logged.
            try:
                self._job_definitions.deregister(previous_definition_arn)
            except Exception as error:
                logger.warning(
                    "job_definition_not_deregistered",
                    extra={"connector_id": connector.connector_id, "error_type": type(error).__name__},
                )
        return UpdateCustomConnectorResponseContent(
            connector=ConnectorSummary.model_validate(updated, from_attributes=True)
        )


class DeleteCustomConnectorActivity:
    """Sets the custom connector `DELETING` and starts the connector deletion workflow."""

    def __init__(
        self,
        connectors: CustomConnectorsDao,
        destinations: DestinationsDao,
        executions: WorkflowExecutions,
        runtime_roles: ConnectorRuntimeRoleLifecycle,
    ) -> None:
        self._connectors = connectors
        self._destinations = destinations
        self._executions = executions
        self._runtime_roles = runtime_roles

    def delete(self, request: OperationRequest[DeleteCustomConnectorRequest]) -> None:
        """Start the deletion, or return when a deletion execution is already running."""
        connector = ConnectorRef.of(request)
        stored = self._connectors.get(connector)
        destination = self._destinations.get(connector.destination)
        bucket_name = destination.destination_resources.bucket_name
        if bucket_name is None:
            raise ResourceConflictError("Destination has no bucket")
        match stored.status:
            case ConnectorStatus.IN_USE:
                raise ResourceConflictError("A job of the custom connector is running")
            case ConnectorStatus.AVAILABLE:
                self._connectors.begin_deletion(connector)
            case ConnectorStatus.DELETING if stored.deletion_execution_arn is not None and self._executions.is_running(
                stored.deletion_execution_arn
            ):
                return
        trace = request.trace
        execution_arn = self._executions.start(
            ConnectorDeletionWorkflowInput(
                tenant_arn_prefix=connector.tenant.arn_prefix,
                destination_id=connector.destination_id,
                connector_id=connector.connector_id,
                connector_arn=connector.arn,
                destination_connector_key=connector.destination_connector_key,
                destination_type=destination.type,
                bucket_name=bucket_name,
                destination_role_arn=destination.role_arn,
                runtime_role_name=self._runtime_roles.role_name(connector),
                schedule_name=connector.schedule_name,
                job_definition_arn=stored.job_definition_arn,
                correlation_id=trace.correlation_id,
                api_request_id=trace.api_request_id,
            )
        )
        self._connectors.record_deletion_execution(
            DeletionExecutionRecord(connector=connector, execution_arn=execution_arn)
        )
