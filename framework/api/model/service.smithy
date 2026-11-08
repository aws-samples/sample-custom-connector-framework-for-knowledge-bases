$version: "2.0"

// The validator guesses that any member named after a parent identifier points at
// every child resource; these members name the destination or custom connector itself.
metadata suppressions = [
    {
        id: "MemberShouldReferenceResource"
        namespace: "ccf"
        reason: "Identifier members name their own resource, not the child resources that share the identifier."
    }
]

namespace ccf

use aws.api#service
use aws.auth#sigv4
use aws.protocols#restJson1

/// Who may call an operation: a tenant principal from an allowed account, a custom
/// connector's runtime role during a job, or both. The backend's caller check and the
/// runtime role's API policy both read this trait from the generated OpenAPI document.
@trait(selector: "operation")
@uniqueItems
list callers {
    member: Caller
}

enum Caller {
    TENANT
    RUNTIME
}

/// The Custom Connector Framework (CCF) API. A tenant creates destinations in its
/// own account and custom connectors that write documents into them; CCF runs each
/// custom connector job as a container on AWS Batch.
///
/// Callers sign with SigV4 (signing name execute-api). OpenAPI and the Pydantic
/// models are generated from this model.
@service(sdkId: "ccf", arnNamespace: "execute-api")
@sigv4(name: "execute-api")
@restJson1
@title("Custom Connector Framework API")
service CustomConnectorFramework {
    version: "2026-11-01"
    resources: [
        DestinationResource
    ]
    errors: [
        BadRequestException
        UnauthorizedException
        ThrottlingException
        ServiceUnavailableException
        InternalServerError
    ]
}

resource DestinationResource {
    identifiers: { destination_id: DestinationId }
    create: CreateDestination
    read: GetDestination
    update: UpdateDestination
    delete: DeleteDestination
    list: ListDestinations
    resources: [
        CustomConnectorResource
    ]
}

resource CustomConnectorResource {
    identifiers: { destination_id: DestinationId, connector_id: ConnectorId }
    create: CreateCustomConnector
    read: GetCustomConnector
    update: UpdateCustomConnector
    delete: DeleteCustomConnector
    list: ListCustomConnectors
    operations: [
        PutCustomConnectorCheckpoint
        GetCustomConnectorCheckpoint
        DeleteCustomConnectorCheckpoint
    ]
    resources: [
        JobResource
        CollectionResource
        DocumentResource
    ]
}

resource JobResource {
    identifiers: { destination_id: DestinationId, connector_id: ConnectorId, job_id: JobId }
    create: StartCustomConnectorJob
    read: GetCustomConnectorJob
    list: ListCustomConnectorJobs
    operations: [
        StopCustomConnectorJob
    ]
}

resource CollectionResource {
    identifiers: { destination_id: DestinationId, connector_id: ConnectorId, collection_id: CollectionId }
    list: ListCustomConnectorCollections
    collectionOperations: [
        BatchDeleteCustomConnectorCollections
    ]
}

/// A child of the custom connector, not of the collection: its paths sit under the
/// custom connector and take collection_id in the body or query string.
resource DocumentResource {
    identifiers: { destination_id: DestinationId, connector_id: ConnectorId, document_id: DocumentId }
    list: ListCustomConnectorDocuments
    collectionOperations: [
        BatchPutCustomConnectorDocuments
        BatchDeleteCustomConnectorDocuments
    ]
}

/// Save the destination and start the workflow that creates its bucket and, for
/// BEDROCK, its knowledge base and data source in the tenant's account.
@http(method: "POST", uri: "/api/v1/destinations", code: 201)
@callers(["TENANT"])
operation CreateDestination {
    input: CreateDestinationRequest
    output: CreateDestinationResponse
    errors: [
        ConflictException
    ]
}

@readonly
@http(method: "GET", uri: "/api/v1/destinations/{destination_id}", code: 200)
@callers(["TENANT", "RUNTIME"])
operation GetDestination {
    input: GetDestinationRequest
    output: GetDestinationResponse
    errors: [
        ResourceNotFoundException
    ]
}

@readonly
@http(method: "GET", uri: "/api/v1/destinations", code: 200)
@callers(["TENANT"])
@paginated(inputToken: "next_token", outputToken: "next_token", pageSize: "max_results", items: "destinations")
operation ListDestinations {
    input: ListDestinationsRequest
    output: ListDestinationsResponse
}

/// Set a BEDROCK destination's custom metadata fields on its knowledge base, then store them. The destination stays
/// ACTIVE, so jobs keep writing. Conflicts while the destination isn't ACTIVE, and while the knowledge base is still
/// applying an earlier update.
@idempotent
@http(method: "PUT", uri: "/api/v1/destinations/{destination_id}", code: 200)
@callers(["TENANT"])
operation UpdateDestination {
    input: UpdateDestinationRequest
    output: UpdateDestinationResponse
    errors: [
        ConflictException
        ResourceNotFoundException
    ]
}

/// Start the workflow that deletes the destination's resources and then its record.
/// Fails while the destination is being created or still holds a custom connector.
@idempotent
@http(method: "DELETE", uri: "/api/v1/destinations/{destination_id}", code: 202)
@callers(["TENANT"])
operation DeleteDestination {
    input: DeleteDestinationRequest
    errors: [
        ConflictException
        ResourceNotFoundException
    ]
}

@http(method: "POST", uri: "/api/v1/destinations/{destination_id}/custom-connectors", code: 201)
@callers(["TENANT"])
operation CreateCustomConnector {
    input: CreateCustomConnectorRequest
    output: CreateCustomConnectorResponse
    errors: [
        ConflictException
        ResourceNotFoundException
    ]
}

@readonly
@http(method: "GET", uri: "/api/v1/destinations/{destination_id}/custom-connectors/{connector_id}", code: 200)
@callers(["TENANT", "RUNTIME"])
operation GetCustomConnector {
    input: GetCustomConnectorRequest
    output: GetCustomConnectorResponse
    errors: [
        ResourceNotFoundException
    ]
}

@readonly
@http(method: "GET", uri: "/api/v1/destinations/{destination_id}/custom-connectors", code: 200)
@callers(["TENANT"])
@paginated(inputToken: "next_token", outputToken: "next_token", pageSize: "max_results", items: "connectors")
operation ListCustomConnectors {
    input: ListCustomConnectorsRequest
    output: ListCustomConnectorsResponse
    errors: [
        ResourceNotFoundException
    ]
}

/// Replace each member sent and keep each member left out.
@idempotent
@http(method: "PUT", uri: "/api/v1/destinations/{destination_id}/custom-connectors/{connector_id}", code: 200)
@callers(["TENANT"])
operation UpdateCustomConnector {
    input: UpdateCustomConnectorRequest
    output: UpdateCustomConnectorResponse
    errors: [
        ConflictException
        ResourceNotFoundException
    ]
}

/// Start the workflow that deletes the custom connector's documents, collections,
/// runtime role, schedule, and then its record.
@idempotent
@http(method: "DELETE", uri: "/api/v1/destinations/{destination_id}/custom-connectors/{connector_id}", code: 202)
@callers(["TENANT"])
operation DeleteCustomConnector {
    input: DeleteCustomConnectorRequest
    errors: [
        ConflictException
        ResourceNotFoundException
    ]
}

@http(method: "POST", uri: "/api/v1/destinations/{destination_id}/custom-connectors/{connector_id}/jobs", code: 201)
@callers(["TENANT"])
operation StartCustomConnectorJob {
    input: StartCustomConnectorJobRequest
    output: StartCustomConnectorJobResponse
    errors: [
        ConflictException
        ResourceNotFoundException
    ]
}

@readonly
@http(
    method: "GET"
    uri: "/api/v1/destinations/{destination_id}/custom-connectors/{connector_id}/jobs/{job_id}"
    code: 200
)
@callers(["TENANT", "RUNTIME"])
operation GetCustomConnectorJob {
    input: GetCustomConnectorJobRequest
    output: GetCustomConnectorJobResponse
    errors: [
        ResourceNotFoundException
    ]
}

@readonly
@http(method: "GET", uri: "/api/v1/destinations/{destination_id}/custom-connectors/{connector_id}/jobs", code: 200)
@callers(["TENANT"])
@paginated(inputToken: "next_token", outputToken: "next_token", pageSize: "max_results", items: "jobs")
operation ListCustomConnectorJobs {
    input: ListCustomConnectorJobsRequest
    output: ListCustomConnectorJobsResponse
    errors: [
        ResourceNotFoundException
    ]
}

/// Stop the job's execution, which asks AWS Batch to stop the job, and set the job STOPPED. A job that
/// already ended conflicts.
@idempotent
@suppress(["HttpMethodSemantics.UnnecessaryIdempotentTrait"])
@http(
    method: "POST"
    uri: "/api/v1/destinations/{destination_id}/custom-connectors/{connector_id}/jobs/{job_id}/stop"
    code: 202
)
@callers(["TENANT"])
operation StopCustomConnectorJob {
    input: StopCustomConnectorJobRequest
    errors: [
        ConflictException
        ResourceNotFoundException
    ]
}

/// Record up to 99 documents whose files the caller already wrote to their final
/// keys, all or none. Recording the same documents again is safe to retry; it keeps each
/// record's `created_at` and refreshes `updated_at`.
@idempotent
@suppress(["HttpMethodSemantics.UnnecessaryIdempotentTrait"])
@http(
    method: "POST"
    uri: "/api/v1/destinations/{destination_id}/custom-connectors/{connector_id}/documents/batch-put"
    code: 204
)
@callers(["RUNTIME"])
operation BatchPutCustomConnectorDocuments {
    input: BatchPutCustomConnectorDocumentsRequest
    errors: [
        ConflictException
        ResourceNotFoundException
    ]
}

/// Delete the files and records of up to 99 documents. Unknown document IDs are
/// ignored, so a retry succeeds.
@idempotent
@suppress(["HttpMethodSemantics.UnnecessaryIdempotentTrait"])
@http(
    method: "POST"
    uri: "/api/v1/destinations/{destination_id}/custom-connectors/{connector_id}/documents/batch-delete"
    code: 204
)
@callers(["RUNTIME"])
operation BatchDeleteCustomConnectorDocuments {
    input: BatchDeleteCustomConnectorDocumentsRequest
    errors: [
        ConflictException
        ResourceNotFoundException
    ]
}

/// List recorded documents in collection ID and then document ID order, optionally in one collection.
@readonly
@http(method: "GET", uri: "/api/v1/destinations/{destination_id}/custom-connectors/{connector_id}/documents", code: 200)
@callers(["TENANT", "RUNTIME"])
@paginated(inputToken: "next_token", outputToken: "next_token", pageSize: "max_results", items: "documents")
operation ListCustomConnectorDocuments {
    input: ListCustomConnectorDocumentsRequest
    output: ListCustomConnectorDocumentsResponse
    errors: [
        ResourceNotFoundException
    ]
}

@readonly
@http(
    method: "GET"
    uri: "/api/v1/destinations/{destination_id}/custom-connectors/{connector_id}/collections"
    code: 200
)
@callers(["TENANT", "RUNTIME"])
@paginated(inputToken: "next_token", outputToken: "next_token", pageSize: "max_results", items: "collections")
operation ListCustomConnectorCollections {
    input: ListCustomConnectorCollectionsRequest
    output: ListCustomConnectorCollectionsResponse
    errors: [
        ResourceNotFoundException
    ]
}

/// Delete up to 100 collections that hold no documents, all or none. Unknown
/// collection IDs are ignored.
@idempotent
@suppress(["HttpMethodSemantics.UnnecessaryIdempotentTrait"])
@http(
    method: "POST"
    uri: "/api/v1/destinations/{destination_id}/custom-connectors/{connector_id}/collections/batch-delete"
    code: 204
)
@callers(["RUNTIME"])
operation BatchDeleteCustomConnectorCollections {
    input: BatchDeleteCustomConnectorCollectionsRequest
    errors: [
        ConflictException
        ResourceNotFoundException
    ]
}

/// Replace the collection's checkpoint. CCF checks only its size and never parses it.
@idempotent
@http(method: "PUT", uri: "/api/v1/destinations/{destination_id}/custom-connectors/{connector_id}/checkpoint", code: 204)
@callers(["TENANT", "RUNTIME"])
operation PutCustomConnectorCheckpoint {
    input: PutCustomConnectorCheckpointRequest
    errors: [
        ConflictException
        ResourceNotFoundException
    ]
}

@readonly
@http(method: "GET", uri: "/api/v1/destinations/{destination_id}/custom-connectors/{connector_id}/checkpoint", code: 200)
@callers(["TENANT", "RUNTIME"])
operation GetCustomConnectorCheckpoint {
    input: GetCustomConnectorCheckpointRequest
    output: GetCustomConnectorCheckpointResponse
    errors: [
        ResourceNotFoundException
    ]
}

/// Remove the collection's checkpoint so the next job reads the source from the
/// start. Fails while a job runs; succeeds when no checkpoint is stored.
@idempotent
@http(
    method: "DELETE"
    uri: "/api/v1/destinations/{destination_id}/custom-connectors/{connector_id}/checkpoint"
    code: 204
)
@callers(["TENANT"])
operation DeleteCustomConnectorCheckpoint {
    input: DeleteCustomConnectorCheckpointRequest
    errors: [
        ConflictException
        ResourceNotFoundException
    ]
}
