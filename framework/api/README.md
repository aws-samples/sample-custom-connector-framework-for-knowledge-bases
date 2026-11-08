# API model

This folder is the source of the framework API's contract. The Smithy model in
[`model/`](./model) declares the 22 operations, their shapes, HTTP bindings,
errors, and constraints. The build turns it into the OpenAPI document that API
Gateway serves and validates requests with. It also generates the Pydantic
models in [`packages/ccf-api-models`](./packages/ccf-api-models) that the
backend, the toolkit's `CcfClient`, and the CDK custom resources use. The repository keeps
no other list of operations or routes.

## Build and test

`make install` syncs the Python environment and downloads the pinned Smithy CLI,
checking the release's SHA-256. `make build` runs the generation chain:

1. `smithy ast` writes `generated/model.json`.
1. `smithy build` writes the OpenAPI document, which the build copies to
   `generated/openapi.json`. `codegen/enrich_openapi.py` then adds the API
   Gateway request validator, the Lambda integration placeholder, and the
   `x-ccf-*` metadata the client and the CDK app read: which members sit in the
   path, query, header, or body, which operations a runtime role may call, and
   which are safe to retry.
1. `datamodel-codegen` writes the strict Pydantic models to
   `packages/ccf-api-models/src/ccf_api_models/models.py`.

`make check` validates the Smithy model, regenerates the contract into
`build/check/`, and fails when the tracked generated files differ from it. It
lints and type-checks `codegen/` and the models package's `base.py`, runs the
generated-model assertions, and then runs `codegen/assert_contract.py`, which checks
each operation's members, locations, and required fields, and the retry-safe
set.
`make test` exercises the generated models. `generated/` is build output and
isn't checked in. Change the Smithy model, never the generated files.

## Operations and fields

The table lists every operation with its request members and its response,
taken from `generated/openapi.json`. A `?` marks an optional member. The
[user guide](../../docs/user-guide.md#call-the-api) explains each operation,
its callers, and its errors.

<!-- api-fields:start -->

| Operation | Method and path | Request members | Response |
| --- | --- | --- | --- |
| `BatchDeleteCustomConnectorCollections` | `POST /api/v1/destinations/{destination_id}/custom-connectors/{connector_id}/collections/batch-delete` | `collections`, `destination_id`, `connector_id` | 204, no body |
| `BatchDeleteCustomConnectorDocuments` | `POST /api/v1/destinations/{destination_id}/custom-connectors/{connector_id}/documents/batch-delete` | `collection_id?`, `document_ids`, `destination_id`, `connector_id` | 204, no body |
| `BatchPutCustomConnectorDocuments` | `POST /api/v1/destinations/{destination_id}/custom-connectors/{connector_id}/documents/batch-put` | `collection_id?`, `source_collection_id?`, `documents`, `destination_id`, `connector_id` | 204, no body |
| `CreateCustomConnector` | `POST /api/v1/destinations/{destination_id}/custom-connectors` | `name`, `description?`, `container_properties`, `connector_configuration?`, `source_access`, `log_destination?`, `schedule?`, `client_token?`, `destination_id` | 201, `connector` (CreatedConnector) |
| `CreateDestination` | `POST /api/v1/destinations` | `name`, `description?`, `type`, `role_arn`, `access_control_enabled?`, `client_token?` | 201, `destination` (CreatedDestination) |
| `DeleteCustomConnector` | `DELETE /api/v1/destinations/{destination_id}/custom-connectors/{connector_id}` | `destination_id`, `connector_id` | 202, no body |
| `DeleteCustomConnectorCheckpoint` | `DELETE /api/v1/destinations/{destination_id}/custom-connectors/{connector_id}/checkpoint` | `destination_id`, `connector_id`, `collection_id?` | 204, no body |
| `DeleteDestination` | `DELETE /api/v1/destinations/{destination_id}` | `destination_id` | 202, no body |
| `GetCustomConnector` | `GET /api/v1/destinations/{destination_id}/custom-connectors/{connector_id}` | `destination_id`, `connector_id` | 200, `connector` (Connector) |
| `GetCustomConnectorCheckpoint` | `GET /api/v1/destinations/{destination_id}/custom-connectors/{connector_id}/checkpoint` | `destination_id`, `connector_id`, `collection_id?` | 200, `checkpoint` (Checkpoint) |
| `GetCustomConnectorJob` | `GET /api/v1/destinations/{destination_id}/custom-connectors/{connector_id}/jobs/{job_id}` | `destination_id`, `connector_id`, `job_id` | 200, `job` (Job) |
| `GetDestination` | `GET /api/v1/destinations/{destination_id}` | `destination_id`, `connector_id?` | 200, `destination` (Destination) |
| `ListCustomConnectorCollections` | `GET /api/v1/destinations/{destination_id}/custom-connectors/{connector_id}/collections` | `destination_id`, `connector_id`, `max_results?`, `next_token?` | 200, `collections` (list of Collection), `next_token?` |
| `ListCustomConnectorDocuments` | `GET /api/v1/destinations/{destination_id}/custom-connectors/{connector_id}/documents` | `destination_id`, `connector_id`, `collection_id?`, `max_results?`, `next_token?` | 200, `documents` (list of StoredDocument), `next_token?` |
| `ListCustomConnectorJobs` | `GET /api/v1/destinations/{destination_id}/custom-connectors/{connector_id}/jobs` | `destination_id`, `connector_id`, `status?`, `max_results?`, `next_token?` | 200, `jobs` (list of JobSummary), `next_token?` |
| `ListCustomConnectors` | `GET /api/v1/destinations/{destination_id}/custom-connectors` | `destination_id`, `name?`, `max_results?`, `next_token?` | 200, `connectors` (list of ConnectorSummary), `next_token?` |
| `ListDestinations` | `GET /api/v1/destinations` | `name?`, `max_results?`, `next_token?` | 200, `destinations` (list of DestinationSummary), `next_token?` |
| `PutCustomConnectorCheckpoint` | `PUT /api/v1/destinations/{destination_id}/custom-connectors/{connector_id}/checkpoint` | `collection_id?`, `source_collection_id?`, `checkpoint_data`, `destination_id`, `connector_id` | 204, no body |
| `StartCustomConnectorJob` | `POST /api/v1/destinations/{destination_id}/custom-connectors/{connector_id}/jobs` | `job_configuration?`, `client_token?`, `destination_id`, `connector_id` | 201, `job` (JobSummary) |
| `StopCustomConnectorJob` | `POST /api/v1/destinations/{destination_id}/custom-connectors/{connector_id}/jobs/{job_id}/stop` | `destination_id`, `connector_id`, `job_id` | 202, no body |
| `UpdateCustomConnector` | `PUT /api/v1/destinations/{destination_id}/custom-connectors/{connector_id}` | `name?`, `description?`, `container_properties?`, `connector_configuration?`, `source_access?`, `log_destination?`, `schedule?`, `destination_id`, `connector_id` | 200, `connector` (ConnectorSummary) |
| `UpdateDestination` | `PUT /api/v1/destinations/{destination_id}` | `metadata_fields`, `destination_id` | 200, `destination` (Destination) |

| Shape | Members |
| --- | --- |
| `Checkpoint` | `checkpoint_data`, `created_at`, `updated_at` |
| `Collection` | `collection_id`, `source_collection_id?`, `checkpoint_data?`, `created_at`, `updated_at` |
| `CollectionReference` | `collection_id` |
| `Connector` | `connector_id`, `arn`, `destination_id`, `name`, `description?`, `status`, `created_at`, `updated_at`, `runtime_role_arn`, `container_properties`, `connector_configuration?`, `source_access`, `log_destination?`, `schedule?` |
| `ConnectorSchedule` | `expression`, `state`, `timezone?` |
| `ConnectorSummary` | `connector_id`, `arn`, `destination_id`, `name`, `description?`, `status`, `created_at`, `updated_at` |
| `ContainerProperties` | `image_uri`, `command`, `resource_requirements`, `timeout` |
| `CreatedConnector` | `connector_id`, `arn`, `destination_id`, `name`, `description?`, `status`, `created_at`, `updated_at`, `runtime_role_arn` |
| `CreatedDestination` | `destination_id`, `arn`, `name`, `description?`, `type`, `access_control_enabled`, `status`, `created_at`, `updated_at` |
| `Destination` | `destination_id`, `arn`, `name`, `description?`, `type`, `access_control_enabled`, `status`, `status_reason?`, `role_arn`, `destination_resources`, `metadata_fields`, `created_at`, `updated_at` |
| `DestinationResources` | `bucket_arn?`, `knowledge_base_arn?`, `data_source_id?`, `knowledge_base_role_arn?`, `bucket_name?`, `knowledge_base_id?` |
| `DestinationSummary` | `destination_id`, `arn`, `name`, `type`, `status`, `created_at`, `updated_at` |
| `DocumentRecord` | `document_id`, `source_document_id`, `content_hash`, `source_version?` |
| `Job` | `job_id`, `arn`, `status`, `created_at`, `updated_at`, `status_reason?`, `job_configuration?`, `service_execution?` |
| `JobSummary` | `job_id`, `arn`, `status`, `created_at`, `updated_at` |
| `LogDestination` | `type`, `arn`, `role_arn` |
| `MetadataField` | `name`, `type`, `searchability` |
| `ResourceRequirements` | `cpu`, `memory` |
| `ServiceExecution` | `execution_id`, `execution_role_arn`, `job_role_arn`, `started_at?`, `completed_at?` |
| `SourceAccess` | `role_arn`, `secret_arn?`, `external_id` |
| `SourceAccessInput` | `role_arn`, `secret_arn?` |
| `StoredDocument` | `collection_id`, `document_id`, `source_document_id`, `content_hash`, `source_version?`, `created_at`, `updated_at` |
| `UpdateContainerProperties` | `image_uri?`, `command?`, `resource_requirements?`, `timeout?` |

<!-- api-fields:end -->
