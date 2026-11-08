---
description: Every Custom Connector Framework API operation, with its path, request members, and response.
---

# Operations

Every path starts with `/api/v1`. In the paths below, `.../custom-connectors` stands for `/destinations/{destination_id}/custom-connectors`, and the job, document, collection, and checkpoint paths continue from `.../custom-connectors/{connector_id}`. A `?` marks an optional member. The [Developer Guide's concepts](../developer-guide/concepts.md) explain what each resource is for.

## Destinations

| Operation | Request | Response |
| --- | --- | --- |
| `CreateDestination`, `POST /destinations` | `name`, `description?`, `type` (`BEDROCK`, `QUICK`, `S3`), `role_arn` (`arn:<partition>:iam::<your account>:role/ccf/destinations/CcfDestinationRole-<name>`), `access_control_enabled?` (default `false`), `client_token?` | 201, `destination`: `destination_id`, `arn`, `name`, `description?`, `type`, `access_control_enabled`, `status`, `created_at`, `updated_at` |
| `GetDestination`, `GET /destinations/{destination_id}` | `destination_id`. The header `x-ccf-connector-id?` is sent only by a runtime role. | 200, `destination`: the members above plus `status_reason?`, `role_arn`, `metadata_fields`, and `destination_resources` (`bucket_arn?`, `bucket_name?`, `knowledge_base_arn?`, `knowledge_base_id?`, `data_source_id?`, `knowledge_base_role_arn?`, each set once its resource exists) |
| `ListDestinations`, `GET /destinations` | `name?`, `max_results?` (1 to 100), `next_token?` | 200, `destinations` (each `destination_id`, `arn`, `name`, `type`, `status`, `created_at`, `updated_at`), `next_token?` |
| `UpdateDestination`, `PUT /destinations/{destination_id}` | `destination_id`, `metadata_fields` (each `name`, `type` `STRING` or `STRING_LIST`, `searchability` `ENABLED` or `DISABLED`) | 200, `destination`, as `GetDestination` returns it |
| `DeleteDestination`, `DELETE /destinations/{destination_id}` | `destination_id` | 202, no body |

`UpdateDestination` returns:

- `BadRequestException` for an `S3` or `QUICK` destination, or for fields [Amazon Bedrock](https://aws.amazon.com/bedrock/) rejects: a name sent twice, or a field whose type changes.
- `ConflictException` while the destination isn't `ACTIVE`, or for a few seconds after an earlier update while the knowledge base applies it.
- `UnauthorizedException` when the destination role lacks `bedrock:UpdateKnowledgeBase`. Deploy its destination stack again to update the role.

`DeleteDestination` returns `ConflictException` while the destination is being created or still has custom connectors.

## Custom connectors

| Operation | Request | Response |
| --- | --- | --- |
| `CreateCustomConnector`, `POST .../custom-connectors` | `destination_id`, `name`, `description?`, `container_properties` (`image_uri`, `command`, `resource_requirements` {`cpu`, `memory`}, `timeout`), `connector_configuration?`, `source_access` (`role_arn`, `secret_arn?`), `log_destination?` (`type`, `arn`, `role_arn`), `schedule?` (`expression`, `state`, `timezone?`), `client_token?` | 201, `connector`: `connector_id`, `arn`, `destination_id`, `name`, `description?`, `status`, `created_at`, `updated_at`, `runtime_role_arn` |
| `GetCustomConnector`, `GET .../custom-connectors/{connector_id}` | `destination_id`, `connector_id` | 200, `connector`: the members above plus `container_properties`, `connector_configuration?`, `source_access` (`role_arn`, `secret_arn?`, `external_id`), `log_destination?`, `schedule?` |
| `ListCustomConnectors`, `GET .../custom-connectors` | `destination_id`, `name?`, `max_results?`, `next_token?` | 200, `connectors` (each `connector_id`, `arn`, `destination_id`, `name`, `description?`, `status`, `created_at`, `updated_at`), `next_token?` |
| `UpdateCustomConnector`, `PUT .../custom-connectors/{connector_id}` | `destination_id`, `connector_id`, `name?`, `description?`, `container_properties?` (each member optional; `cpu` and `memory` together), `connector_configuration?`, `source_access?`, `log_destination?` (`null` removes it), `schedule?` (`null` deletes it) | 200, `connector`: the summary members |
| `DeleteCustomConnector`, `DELETE .../custom-connectors/{connector_id}` | `destination_id`, `connector_id` | 202, no body |

- `container_properties.image_uri` must be an image in an [Amazon ECR](https://aws.amazon.com/ecr/) repository named `ccf-connectors/<name>`, tagged or pinned by digest. `resource_requirements` takes `cpu`, at least 0.5, and `memory` in MiB, at least 1,024. `timeout` is in seconds, 60 to 604,800.
- `connector_configuration` is a string of at most 65,536 UTF-8 bytes, which the framework encrypts and never parses.
- `schedule.expression` is an [Amazon EventBridge Scheduler expression](https://docs.aws.amazon.com/scheduler/latest/UserGuide/schedule-types.html), such as `cron(0 2 ? * SUN *)`.
- `CreateCustomConnector` returns `ConflictException` while the destination isn't `ACTIVE`.
- `UpdateCustomConnector` replaces only the members you send, and keeps a member you leave out. It returns `ConflictException` while a job runs or the custom connector is being deleted.
- `DeleteCustomConnector` returns `ConflictException` while a job runs.

## Jobs

| Operation | Request | Response |
| --- | --- | --- |
| `StartCustomConnectorJob`, `POST .../jobs` | `destination_id`, `connector_id`, `job_configuration?` (at most 65,536 bytes, never parsed), `client_token?` | 201, `job`: `job_id`, `arn`, `status` (`STARTED`), `created_at`, `updated_at` |
| `GetCustomConnectorJob`, `GET .../jobs/{job_id}` | `destination_id`, `connector_id`, `job_id` | 200, `job`: the members above plus `status_reason?`, `job_configuration?`, `service_execution?` (`execution_id`, `execution_role_arn`, `job_role_arn`, `started_at?`, `completed_at?`) |
| `ListCustomConnectorJobs`, `GET .../jobs` | `destination_id`, `connector_id`, `status?`, `max_results?`, `next_token?` | 200, `jobs` (summaries, newest first), `next_token?` |
| `StopCustomConnectorJob`, `POST .../jobs/{job_id}/stop` | `destination_id`, `connector_id`, `job_id` | 202, no body |

`StartCustomConnectorJob` returns `ConflictException` while another job of the custom connector runs, or while its destination isn't `ACTIVE`. `StopCustomConnectorJob` returns `ConflictException` for a job that already ended.

## Documents, collections, and checkpoints

The toolkit calls the batch operations for a connector implementation. You can call the list and checkpoint operations yourself to inspect a custom connector, or to change its checkpoints between jobs.

| Operation | Request | Response |
| --- | --- | --- |
| `BatchPutCustomConnectorDocuments`, `POST .../documents/batch-put` | `destination_id`, `connector_id`, `collection_id?`, `source_collection_id?`, `documents` (1 to 99, each `document_id`, `source_document_id`, `content_hash`, `source_version?`) | 204, no body |
| `BatchDeleteCustomConnectorDocuments`, `POST .../documents/batch-delete` | `destination_id`, `connector_id`, `collection_id?`, `document_ids` (1 to 99) | 204, no body |
| `ListCustomConnectorDocuments`, `GET .../documents` | `destination_id`, `connector_id`, `collection_id?`, `max_results?`, `next_token?` | 200, `documents` (each `collection_id`, `document_id`, `source_document_id`, `content_hash`, `source_version?`, `created_at`, `updated_at`), `next_token?` |
| `ListCustomConnectorCollections`, `GET .../collections` | `destination_id`, `connector_id`, `max_results?`, `next_token?` | 200, `collections` (each `collection_id`, `source_collection_id?`, `checkpoint_data?`, `created_at`, `updated_at`), `next_token?` |
| `BatchDeleteCustomConnectorCollections`, `POST .../collections/batch-delete` | `destination_id`, `connector_id`, `collections` (1 to 100, each `collection_id`) | 204, no body |
| `PutCustomConnectorCheckpoint`, `PUT .../checkpoint` | `destination_id`, `connector_id`, `collection_id?`, `source_collection_id?`, `checkpoint_data` (2 to 16,384 bytes) | 204, no body |
| `GetCustomConnectorCheckpoint`, `GET .../checkpoint` | `destination_id`, `connector_id`, `collection_id?` | 200, `checkpoint`: `checkpoint_data`, `created_at`, `updated_at` |
| `DeleteCustomConnectorCheckpoint`, `DELETE .../checkpoint` | `destination_id`, `connector_id`, `collection_id?` | 204, no body |

A `collection_id` left out means `col-default`. Any other `collection_id` is `col-` plus the first 16 hex characters of the SHA-256 of its `source_collection_id`, which the request must also send. A `document_id` is `doc-` plus the first 16 hex characters of the SHA-256 of its `source_document_id`. A collection that still has documents can't be deleted. After `DeleteCustomConnectorCheckpoint`, the next job gets no checkpoint for that collection, so the implementation starts that collection over.
