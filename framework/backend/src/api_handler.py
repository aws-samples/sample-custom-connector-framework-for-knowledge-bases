"""API Gateway composition root: builds every activity once per cold start and routes the 22 operations.

The catalog gives each operation its route, request model, success status, admitted callers, and
client token member; `ACTIVITIES` gives each its activity. An operation with a client token runs
through idempotency.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

import boto3
from aws_lambda_powertools.event_handler import APIGatewayRestResolver
from aws_lambda_powertools.utilities.typing import LambdaContext
from ccf_api_models.catalog import OperationCatalog
from ccf_api_models.identifiers import ConnectorRef, DestinationRef
from ccf_api_models.operations import Operation

from activities.checkpoints import (
    DeleteCustomConnectorCheckpointActivity,
    GetCustomConnectorCheckpointActivity,
    PutCustomConnectorCheckpointActivity,
)
from activities.collections import BatchDeleteCustomConnectorCollectionsActivity
from activities.connectors import (
    CreateCustomConnectorActivity,
    DeleteCustomConnectorActivity,
    GetCustomConnectorActivity,
    UpdateCustomConnectorActivity,
)
from activities.destinations import CreateDestinationActivity, DeleteDestinationActivity, UpdateDestinationActivity
from activities.documents import BatchDeleteCustomConnectorDocumentsActivity, BatchPutCustomConnectorDocumentsActivity
from activities.jobs import (
    GetCustomConnectorJobActivity,
    StartCustomConnectorJobActivity,
    StopCustomConnectorJobActivity,
)
from common.composition import DataStores, DataStoresRequest, JobsDaoRequest, build_jobs_dao
from common.connector_job_definitions import ConnectorJobDefinitions
from common.delegation import TenantResolver
from common.external_ids import ExternalIdGenerator
from common.idempotency import IdempotentCreates
from common.observability import ServiceMetrics, logger, tracer
from common.operation import Activity
from common.read_resources import ListResources, ReadResource
from common.request_binder import RequestBinder
from common.resource_data_codec import KmsResourceDataCodec
from common.resource_ids import ResourceIds
from common.router import RouterAdapter
from common.routes import RouteTable
from common.runtime_role import ConnectorRuntimeRoleLifecycle
from common.schedule_lifecycle import ScheduleLifecycle
from common.settings import ApiSettings
from common.storage.bedrock.knowledge_base_fields_dao import KnowledgeBaseFieldsDao
from common.storage.s3.destination_files_dao import DestinationFilesDao
from common.tenant_role_clients import TenantRoleClients
from common.validators.connector_request_validator import ConnectorRequestValidator
from common.validators.connector_write_gate import ConnectorWriteGate
from common.validators.create_destination_validator import CreateDestinationValidator
from common.validators.source_identifiers import CollectionIdentifierValidator, DocumentIdentifierValidator
from common.workflow_executions import WorkflowExecutions

settings = ApiSettings()
app = APIGatewayRestResolver()
catalog = OperationCatalog.load()
codec = KmsResourceDataCodec.for_key(settings.resource_data_key_arn)
stores = DataStores.build(DataStoresRequest(tables=settings.tables, codec=codec))
jobs = build_jobs_dao(JobsDaoRequest(tables=settings.tables, jobs=settings.jobs))
idempotency = IdempotentCreates.from_settings(settings)
metrics = ServiceMetrics()
stepfunctions = boto3.client("stepfunctions")
destination_workflow = WorkflowExecutions(stepfunctions, settings.workflows.destination)
job_executions = WorkflowExecutions(stepfunctions, settings.jobs.job_execution_arn)
runtime_roles = ConnectorRuntimeRoleLifecycle(boto3.client("iam"), settings.runtime_roles, settings.service)
schedules = ScheduleLifecycle(boto3.client("scheduler"), settings.scheduler)
job_definitions = ConnectorJobDefinitions(boto3.client("batch"), settings.job_definitions)
connector_validator = ConnectorRequestValidator(settings.aws_region, settings.aws_url_suffix)
write_gate = ConnectorWriteGate(stores.destinations)
collection_identifiers = CollectionIdentifierValidator()
resource_ids = ResourceIds()
tenant_roles = TenantRoleClients(boto3.client("sts"), settings.aws_region)

create_destination = CreateDestinationActivity(
    CreateDestinationValidator(), stores.destinations, destination_workflow, resource_ids
)
create_connector = CreateCustomConnectorActivity(
    stores.connectors,
    stores.destinations,
    connector_validator,
    codec,
    runtime_roles,
    schedules,
    job_definitions,
    ExternalIdGenerator(),
    resource_ids,
)
update_connector = UpdateCustomConnectorActivity(
    stores.connectors, connector_validator, codec, runtime_roles, schedules, job_definitions
)
delete_connector = DeleteCustomConnectorActivity(
    stores.connectors,
    stores.destinations,
    WorkflowExecutions(stepfunctions, settings.workflows.connector_deletion),
    runtime_roles,
)
batch_put_documents = BatchPutCustomConnectorDocumentsActivity(
    write_gate, stores.collections, stores.documents, collection_identifiers, DocumentIdentifierValidator(), metrics
)
batch_delete_documents = BatchDeleteCustomConnectorDocumentsActivity(
    write_gate,
    stores.collections,
    stores.documents,
    DestinationFilesDao(tenant_roles, settings.workflows.destination),
    metrics,
)

ACTIVITIES: Final[Mapping[Operation, Activity]] = {
    Operation.CREATE_DESTINATION: create_destination.create,
    Operation.GET_DESTINATION: ReadResource(
        lambda destination: stores.destinations.get(destination).destination(),
        DestinationRef.of,
        catalog.spec(Operation.GET_DESTINATION),
    ).read,
    Operation.LIST_DESTINATIONS: ListResources(
        stores.destinations.list, lambda request: request.tenant, catalog.spec(Operation.LIST_DESTINATIONS)
    ).list,
    Operation.UPDATE_DESTINATION: UpdateDestinationActivity(
        stores.destinations, KnowledgeBaseFieldsDao(tenant_roles, settings.workflows.destination)
    ).update,
    Operation.DELETE_DESTINATION: DeleteDestinationActivity(
        stores.destinations, stores.connectors, destination_workflow
    ).delete,
    Operation.CREATE_CUSTOM_CONNECTOR: create_connector.create,
    Operation.GET_CUSTOM_CONNECTOR: GetCustomConnectorActivity(stores.connectors, codec).fetch,
    Operation.LIST_CUSTOM_CONNECTORS: ListResources(
        stores.connectors.list,
        DestinationRef.of,
        catalog.spec(Operation.LIST_CUSTOM_CONNECTORS),
        stores.destinations.exists,
    ).list,
    Operation.UPDATE_CUSTOM_CONNECTOR: update_connector.update,
    Operation.DELETE_CUSTOM_CONNECTOR: delete_connector.delete,
    Operation.START_CUSTOM_CONNECTOR_JOB: StartCustomConnectorJobActivity(
        jobs, codec, job_executions, resource_ids
    ).start,
    Operation.GET_CUSTOM_CONNECTOR_JOB: GetCustomConnectorJobActivity(jobs, codec).fetch,
    Operation.LIST_CUSTOM_CONNECTOR_JOBS: ListResources(
        jobs.list, ConnectorRef.of, catalog.spec(Operation.LIST_CUSTOM_CONNECTOR_JOBS), stores.connectors.exists
    ).list,
    Operation.STOP_CUSTOM_CONNECTOR_JOB: StopCustomConnectorJobActivity(jobs, job_executions, metrics).stop,
    Operation.BATCH_PUT_CUSTOM_CONNECTOR_DOCUMENTS: batch_put_documents.put,
    Operation.BATCH_DELETE_CUSTOM_CONNECTOR_DOCUMENTS: batch_delete_documents.delete,
    Operation.LIST_CUSTOM_CONNECTOR_DOCUMENTS: ListResources(
        stores.documents.list,
        ConnectorRef.of,
        catalog.spec(Operation.LIST_CUSTOM_CONNECTOR_DOCUMENTS),
        stores.connectors.exists,
    ).list,
    Operation.LIST_CUSTOM_CONNECTOR_COLLECTIONS: ListResources(
        stores.collections.list,
        ConnectorRef.of,
        catalog.spec(Operation.LIST_CUSTOM_CONNECTOR_COLLECTIONS),
        stores.connectors.exists,
    ).list,
    Operation.BATCH_DELETE_CUSTOM_CONNECTOR_COLLECTIONS: BatchDeleteCustomConnectorCollectionsActivity(
        stores.collections, stores.documents, metrics
    ).delete,
    Operation.PUT_CUSTOM_CONNECTOR_CHECKPOINT: PutCustomConnectorCheckpointActivity(
        stores.collections, collection_identifiers
    ).put,
    Operation.GET_CUSTOM_CONNECTOR_CHECKPOINT: GetCustomConnectorCheckpointActivity(stores.collections).fetch,
    Operation.DELETE_CUSTOM_CONNECTOR_CHECKPOINT: DeleteCustomConnectorCheckpointActivity(stores.collections).delete,
}

router = RouterAdapter(app, RequestBinder(app), TenantResolver(stores.connectors, settings), idempotency, metrics)
RouteTable(app, catalog, router).register(ACTIVITIES)


@logger.inject_lambda_context(clear_state=True)
@tracer.capture_lambda_handler
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """Route one API Gateway request; its body and headers are never logged."""
    idempotency.register_lambda_context(context)
    logger.info(
        "api_request_received", extra={"http_method": event.get("httpMethod"), "resource": event.get("resource")}
    )
    return app.resolve(event, context)
