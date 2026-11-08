---
description: The Custom Connector Framework's components, stacks, API resources, and data model.
---

# Architecture

The Custom Connector Framework runs connector code it doesn't trust in the service account, against resources the tenant owns in its own account. That constraint shapes most of the design: which role reaches which account, what the backend checks, and what the toolkit does for the connector. [How it works](../getting-started/how-it-works.md) shows the end-to-end flow, and [Security](../administrator-guide/security.md) shows the account boundary and the network.

## Components

| Component | Code | What it owns |
| --- | --- | --- |
| Framework service | `framework/backend`, and the `CustomConnectorFramework` and `CustomConnectorFrameworkOps` stacks in `framework/cdk` | The API and its state, destinations and their workflows, scheduling, the `CcfJobExecution` [AWS Step Functions](https://aws.amazon.com/step-functions/) state machine that runs every job, [AWS Batch](https://aws.amazon.com/batch/) compute and job definitions with the log router, runtime roles and their boundary, the Batch execution role, external IDs, the ingestion queue, and the alarms and dashboard |
| API model | `framework/api` | The Smithy model, the generated OpenAPI document, and the generated Pydantic models every other component uses |
| Toolkit | `framework/toolkit`, and `framework/cdk/lib/index.ts` | `CcfClient`, the job runner, `DocumentStore`, metadata files and access control lists, media type detection, the `testing` module, and the CDK constructs: `FrameworkValues`, `DestinationRole`, `Destination`, `ConnectorImage`, `CustomConnector`, `CustomConnectorDestination`, `CustomConnectorProject`, and the destination stack |
| Example GitHub connector | `connectors/github` | GitHub retrieval, its configuration format and checkpoints, the token secret, and its CDK app |

The service account holds the framework, which deploys first. Each destination stack, then each connector project, deploys in a tenant account. A connector stack names an existing destination by its ID.

## API resources

A destination holds custom connectors. A custom connector holds jobs, collections, and documents, and a collection carries a checkpoint.

| Resource | Operations |
| --- | --- |
| Destination | `CreateDestination`, `GetDestination`, `ListDestinations`, `UpdateDestination`, `DeleteDestination` |
| Custom connector | `CreateCustomConnector`, `GetCustomConnector`, `ListCustomConnectors`, `UpdateCustomConnector`, `DeleteCustomConnector` |
| Job | `StartCustomConnectorJob`, `GetCustomConnectorJob`, `ListCustomConnectorJobs`, `StopCustomConnectorJob` |
| Collection | `ListCustomConnectorCollections`, `BatchDeleteCustomConnectorCollections` |
| Checkpoint | `PutCustomConnectorCheckpoint`, `GetCustomConnectorCheckpoint`, `DeleteCustomConnectorCheckpoint` |
| Document | `BatchPutCustomConnectorDocuments`, `BatchDeleteCustomConnectorDocuments`, `ListCustomConnectorDocuments` |

Document paths sit under the custom connector and take `collection_id` in the body or query, so the Smithy model makes a document a child of the custom connector. The document ARN still nests a document under its collection.

A runtime role's call carries the `x-ccf-tenant-arn-prefix` header. The backend accepts it only from the runtime role stored for the custom connector in the path. For `GetDestination`, the custom connector is the one in `x-ccf-connector-id`, and it must belong to the path's destination. [API Reference](../api-reference/index.md#callers) lists which caller may call each operation.

## Data model

Every table is an [Amazon DynamoDB](https://aws.amazon.com/dynamodb/) table with on-demand capacity, point-in-time recovery, and encryption under the framework's [AWS KMS](https://aws.amazon.com/kms/) key. The configuration's `removalPolicy` decides whether they're kept when the stack is deleted. Reads that gate a write or a run are strongly consistent.

| Table | Partition key | Sort key | Index | Holds |
| --- | --- | --- | --- | --- |
| `CcfDestinations` | `tenant_arn_prefix` | `destination_id` | `ByName` (`tenant_arn_prefix`, `name`) | Destinations, their status, their resources, their metadata fields, the pending ingestion token, and the last workflow execution |
| `CcfCustomConnectors` | `tenant_arn_prefix` | `destination_connector_key` (`<dst>#<cc>`) | `ByName` (`tenant_arn_prefix`, `destination_name_key`) | Custom connectors, with encrypted configuration, external ID, source access, log destination, and the active job |
| `CcfCustomConnectorJobs` | `tenant_arn_prefix` | `job_id` | `ByConnectorSummary` (`connector_key`, the custom connector ARN; `created_at`) | Jobs, with encrypted job configuration. Items expire 7 days after the job ends. |
| `CcfCollections` | `connector_arn` | `collection_id` | None | Collections, their source collection IDs, encrypted checkpoints, and delete marks |
| `CcfDocuments` | `connector_arn` | `collection_document_key` (`<col>#<doc>`) | None | Document records: source document ID, content hash, source version |
| `CcfIdempotency` | `id` | None | None | Idempotency records for `CreateDestination`, `CreateCustomConnector`, and `StartCustomConnectorJob`, expiring after one day. The key is a hash of the tenant, the operation, and the token, so two tenants with one token get two resources. |
