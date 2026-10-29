"""Thin API Gateway composition root for generated CCF operation models."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any, cast

import boto3
from aws_cryptographic_material_providers.mpl import (  # type: ignore[import-untyped]
    AwsCryptographicMaterialProviders,
)
from aws_cryptographic_material_providers.mpl.config import (  # type: ignore[import-untyped]
    MaterialProvidersConfig,
)
from aws_cryptographic_material_providers.mpl.models import (  # type: ignore[import-untyped]
    CreateAwsKmsKeyringInput,
)
from aws_lambda_powertools.event_handler import APIGatewayRestResolver, Response
from aws_lambda_powertools.utilities.typing import LambdaContext
from ccf_api_models.models import (
    BatchDeleteCustomConnectorDocumentsRequest,
    BatchPutCustomConnectorDocumentsRequest,
    CreateCustomConnectorRequest,
    DeleteCustomConnectorCheckpointRequest,
    DeleteCustomConnectorRequest,
    DestinationMode,
    GetCustomConnectorCheckpointRequest,
    GetCustomConnectorJobRequest,
    GetCustomConnectorRequest,
    ListCustomConnectorDocumentsRequest,
    ListCustomConnectorJobsRequest,
    ListCustomConnectorsRequest,
    PutCustomConnectorCheckpointRequest,
    StartCustomConnectorJobRequest,
    StopCustomConnectorJobRequest,
    UpdateCustomConnectorRequest,
)

from activities.checkpoints import (
    DeleteCustomConnectorCheckpointActivity,
    GetCustomConnectorCheckpointActivity,
    PutCustomConnectorCheckpointActivity,
)
from activities.connectors import (
    ConnectorOwnershipValidator,
    CreateCustomConnectorActivity,
    DeleteCustomConnectorActivity,
    GetCustomConnectorActivity,
    ListCustomConnectorsActivity,
    ServiceReferenceConfiguration,
    UpdateCustomConnectorActivity,
)
from activities.documents import (
    BatchDeleteCustomConnectorDocumentsActivity,
    BatchPutCustomConnectorDocumentsActivity,
    ListCustomConnectorDocumentsActivity,
)
from activities.jobs import (
    GetCustomConnectorJobActivity,
    ListCustomConnectorJobsActivity,
    StartCustomConnectorJobActivity,
    StopCustomConnectorJobActivity,
)
from common.configuration import ConfigurationValidator, KmsConfigurationCodec, create_encryption_client
from common.delegation import TenantResolver
from common.env import (
    ALLOWED_CLIENT_ACCOUNT_IDS,
    AWS_PARTITION,
    AWS_REGION,
    CCF_SERVICE_PRINCIPAL,
    CONFIGURATION_KEY_ARN,
    CONNECTOR_RUNTIME_ROUTES,
    CUSTOM_CONNECTOR_API_ENDPOINT,
    CUSTOM_CONNECTOR_DOCUMENTS_TABLE_NAME,
    CUSTOM_CONNECTOR_JOBS_TABLE_NAME,
    CUSTOM_CONNECTORS_TABLE_NAME,
    DESTINATION_MODE,
    FRAMEWORK_ACCOUNT_ID,
    INGESTION_BUCKET_NAME,
    RUNTIME_ROLE_BOUNDARY_ARN,
    SCHEDULER_DLQ_ARN,
    SCHEDULER_GROUP_NAME,
    SCHEDULER_ROLE_ARN,
    SCHEDULER_TARGET_ARN,
)
from common.observability import logger
from common.router import RouteInvocation, RouterAdapter
from common.runtime_role import (
    ConnectorRuntimeRoleLifecycle,
    RuntimeRoleDeployment,
    RuntimeRoleIamClient,
    parse_runtime_routes,
)
from common.schedule_lifecycle import ScheduleLifecycle
from common.storage.ddb.custom_connector_documents_dao import CustomConnectorDocumentsDao
from common.storage.ddb.custom_connector_jobs_dao import CustomConnectorJobsDao
from common.storage.ddb.custom_connectors_dao import CustomConnectorsDao

_PAGINATION_QUERY_MEMBERS = frozenset({"max_results", "next_token"})
_JOB_LIST_QUERY_MEMBERS = _PAGINATION_QUERY_MEMBERS | {"status"}

app = APIGatewayRestResolver()
dynamodb = boto3.resource("dynamodb")
connectors_dao = CustomConnectorsDao(dynamodb.Table(CUSTOM_CONNECTORS_TABLE_NAME))
jobs_dao = CustomConnectorJobsDao(
    dynamodb.Table(CUSTOM_CONNECTOR_JOBS_TABLE_NAME),
    connectors_dao,
    dynamodb.meta.client,
)
documents_dao = CustomConnectorDocumentsDao(
    dynamodb.Table(CUSTOM_CONNECTOR_DOCUMENTS_TABLE_NAME),
    connectors_dao,
)
material_providers = AwsCryptographicMaterialProviders(config=MaterialProvidersConfig())
configuration_keyring = material_providers.create_aws_kms_keyring(
    input=CreateAwsKmsKeyringInput(
        kms_key_id=CONFIGURATION_KEY_ARN,
        kms_client=boto3.client("kms", region_name=AWS_REGION),
    )
)
configuration_codec = KmsConfigurationCodec(create_encryption_client(), configuration_keyring)
configuration_validator = ConfigurationValidator()
source_role_account_ids = frozenset(account_id for account_id in ALLOWED_CLIENT_ACCOUNT_IDS.split(",") if account_id)
ownership_validator = ConnectorOwnershipValidator(source_role_account_ids)
runtime_roles = ConnectorRuntimeRoleLifecycle(
    cast(RuntimeRoleIamClient, boto3.client("iam", region_name=AWS_REGION)),
    RuntimeRoleDeployment(
        partition=AWS_PARTITION,
        account_id=FRAMEWORK_ACCOUNT_ID,
        region=AWS_REGION,
        ingestion_bucket_name=INGESTION_BUCKET_NAME,
        destination_key_arn=CONFIGURATION_KEY_ARN,
        permissions_boundary_arn=RUNTIME_ROLE_BOUNDARY_ARN,
        runtime_routes=parse_runtime_routes(CONNECTOR_RUNTIME_ROUTES),
    ),
)
router = RouterAdapter(app, TenantResolver(connectors_dao, FRAMEWORK_ACCOUNT_ID))
schedule_lifecycle = ScheduleLifecycle(
    scheduler_client=boto3.client("scheduler", region_name=AWS_REGION),
    target_arn=SCHEDULER_TARGET_ARN,
    role_arn=SCHEDULER_ROLE_ARN,
    group_name=SCHEDULER_GROUP_NAME,
    dead_letter_arn=SCHEDULER_DLQ_ARN,
)

create_connector_activity = CreateCustomConnectorActivity(
    connectors_dao,
    configuration_validator,
    configuration_codec,
    schedule_lifecycle,
    ownership_validator,
    runtime_roles,
)
get_connector_activity = GetCustomConnectorActivity(
    connectors_dao,
    configuration_codec,
    ServiceReferenceConfiguration(
        account_id=FRAMEWORK_ACCOUNT_ID,
        endpoint=CUSTOM_CONNECTOR_API_ENDPOINT or "",
        region=AWS_REGION,
        partition=AWS_PARTITION,
        service_principal=CCF_SERVICE_PRINCIPAL,
        ingestion_bucket_name=INGESTION_BUCKET_NAME,
        destination_mode=DestinationMode(DESTINATION_MODE),
    ),
)
list_connectors_activity = ListCustomConnectorsActivity(connectors_dao)
update_connector_activity = UpdateCustomConnectorActivity(
    connectors_dao,
    configuration_validator,
    configuration_codec,
    schedule_lifecycle,
    ownership_validator,
    runtime_roles,
)
delete_connector_activity = DeleteCustomConnectorActivity(connectors_dao, schedule_lifecycle, runtime_roles)
start_job_activity = StartCustomConnectorJobActivity(
    jobs_dao,
    configuration_validator,
    configuration_codec,
)
get_job_activity = GetCustomConnectorJobActivity(jobs_dao, configuration_codec)
stop_job_activity = StopCustomConnectorJobActivity(jobs_dao)
list_jobs_activity = ListCustomConnectorJobsActivity(jobs_dao)
put_checkpoint_activity = PutCustomConnectorCheckpointActivity(connectors_dao)
get_checkpoint_activity = GetCustomConnectorCheckpointActivity(connectors_dao)
delete_checkpoint_activity = DeleteCustomConnectorCheckpointActivity(connectors_dao)
batch_put_documents_activity = BatchPutCustomConnectorDocumentsActivity(documents_dao)
batch_delete_documents_activity = BatchDeleteCustomConnectorDocumentsActivity(documents_dao)
list_documents_activity = ListCustomConnectorDocumentsActivity(documents_dao)


@app.post("/api/v1/custom-connectors")
def create_custom_connector() -> Response[str]:
    """Create a connector."""
    return router.execute(
        RouteInvocation(
            "CreateCustomConnector",
            CreateCustomConnectorRequest,
            create_connector_activity.create,
            HTTPStatus.CREATED,
        )
    )


@app.get("/api/v1/custom-connectors/<connector_id>")
def get_custom_connector(connector_id: str) -> Response[str]:
    """Get a connector."""
    del connector_id
    return router.execute(
        RouteInvocation(
            "GetCustomConnector",
            GetCustomConnectorRequest,
            get_connector_activity.fetch,
            HTTPStatus.OK,
        )
    )


@app.get("/api/v1/custom-connectors")
def list_custom_connectors() -> Response[str]:
    """List connectors."""
    return router.execute(
        RouteInvocation(
            "ListCustomConnectors",
            ListCustomConnectorsRequest,
            list_connectors_activity.list,
            HTTPStatus.OK,
            _PAGINATION_QUERY_MEMBERS,
        )
    )


@app.put("/api/v1/custom-connectors/<connector_id>")
def update_custom_connector(connector_id: str) -> Response[str]:
    """Update a connector."""
    del connector_id
    return router.execute(
        RouteInvocation(
            "UpdateCustomConnector",
            UpdateCustomConnectorRequest,
            update_connector_activity.update,
            HTTPStatus.OK,
        )
    )


@app.delete("/api/v1/custom-connectors/<connector_id>")
def delete_custom_connector(connector_id: str) -> Response[str]:
    """Delete a connector."""
    del connector_id
    return router.execute(
        RouteInvocation(
            "DeleteCustomConnector",
            DeleteCustomConnectorRequest,
            delete_connector_activity.delete,
            HTTPStatus.NO_CONTENT,
        )
    )


@app.post("/api/v1/custom-connectors/<connector_id>/jobs")
def start_custom_connector_job(connector_id: str) -> Response[str]:
    """Start a connector job."""
    del connector_id
    return router.execute(
        RouteInvocation(
            "StartCustomConnectorJob",
            StartCustomConnectorJobRequest,
            start_job_activity.start,
            HTTPStatus.CREATED,
        )
    )


@app.get("/api/v1/custom-connectors/<connector_id>/jobs/<job_id>")
def get_custom_connector_job(connector_id: str, job_id: str) -> Response[str]:
    """Get a connector job."""
    del connector_id, job_id
    return router.execute(
        RouteInvocation(
            "GetCustomConnectorJob",
            GetCustomConnectorJobRequest,
            get_job_activity.fetch,
            HTTPStatus.OK,
        )
    )


@app.post("/api/v1/custom-connectors/<connector_id>/jobs/<job_id>/stop")
def stop_custom_connector_job(connector_id: str, job_id: str) -> Response[str]:
    """Stop a connector job."""
    del connector_id, job_id
    return router.execute(
        RouteInvocation(
            "StopCustomConnectorJob",
            StopCustomConnectorJobRequest,
            stop_job_activity.stop,
            HTTPStatus.ACCEPTED,
        )
    )


@app.get("/api/v1/custom-connectors/<connector_id>/jobs")
def list_custom_connector_jobs(connector_id: str) -> Response[str]:
    """List connector jobs."""
    del connector_id
    return router.execute(
        RouteInvocation(
            "ListCustomConnectorJobs",
            ListCustomConnectorJobsRequest,
            list_jobs_activity.list,
            HTTPStatus.OK,
            _JOB_LIST_QUERY_MEMBERS,
        )
    )


@app.post("/api/v1/custom-connectors/<connector_id>/documents")
def batch_put_custom_connector_documents(connector_id: str) -> Response[str]:
    """Put connector document state."""
    del connector_id
    return router.execute(
        RouteInvocation(
            "BatchPutCustomConnectorDocuments",
            BatchPutCustomConnectorDocumentsRequest,
            batch_put_documents_activity.put,
            HTTPStatus.ACCEPTED,
        )
    )


@app.delete("/api/v1/custom-connectors/<connector_id>/documents")
def batch_delete_custom_connector_documents(connector_id: str) -> Response[str]:
    """Delete connector document state."""
    del connector_id
    return router.execute(
        RouteInvocation(
            "BatchDeleteCustomConnectorDocuments",
            BatchDeleteCustomConnectorDocumentsRequest,
            batch_delete_documents_activity.delete,
            HTTPStatus.ACCEPTED,
        )
    )


@app.get("/api/v1/custom-connectors/<connector_id>/documents")
def list_custom_connector_documents(connector_id: str) -> Response[str]:
    """List connector document state."""
    del connector_id
    return router.execute(
        RouteInvocation(
            "ListCustomConnectorDocuments",
            ListCustomConnectorDocumentsRequest,
            list_documents_activity.list,
            HTTPStatus.OK,
            _PAGINATION_QUERY_MEMBERS,
        )
    )


@app.put("/api/v1/custom-connectors/<connector_id>/checkpoint")
def put_custom_connector_checkpoint(connector_id: str) -> Response[str]:
    """Put a connector checkpoint."""
    del connector_id
    return router.execute(
        RouteInvocation(
            "PutCustomConnectorCheckpoint",
            PutCustomConnectorCheckpointRequest,
            put_checkpoint_activity.put,
            HTTPStatus.ACCEPTED,
        )
    )


@app.get("/api/v1/custom-connectors/<connector_id>/checkpoint")
def get_custom_connector_checkpoint(connector_id: str) -> Response[str]:
    """Get a connector checkpoint."""
    del connector_id
    return router.execute(
        RouteInvocation(
            "GetCustomConnectorCheckpoint",
            GetCustomConnectorCheckpointRequest,
            get_checkpoint_activity.fetch,
            HTTPStatus.OK,
        )
    )


@app.delete("/api/v1/custom-connectors/<connector_id>/checkpoint")
def delete_custom_connector_checkpoint(connector_id: str) -> Response[str]:
    """Delete a connector checkpoint."""
    del connector_id
    return router.execute(
        RouteInvocation(
            "DeleteCustomConnectorCheckpoint",
            DeleteCustomConnectorCheckpointRequest,
            delete_checkpoint_activity.delete,
            HTTPStatus.ACCEPTED,
        )
    )


@logger.inject_lambda_context(clear_state=True)
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """Resolve one API Gateway request without logging its body or event."""
    request_context = event.get("requestContext", {})
    request_id = request_context.get("requestId") if isinstance(request_context, dict) else None
    logger.info(
        "api_request_received",
        extra={
            "http_method": event.get("httpMethod"),
            "resource": event.get("resource"),
            "request_id": request_id,
        },
    )
    return app.resolve(event, context)
