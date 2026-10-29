$version: "2.0"

namespace ccf

use aws.api#service
use aws.auth#sigv4
use aws.protocols#restJson1

/// The Custom Connector Framework (CCF) control plane. CCF runs custom data
/// connectors as containerized AWS Batch jobs that land documents in Amazon S3
/// for a deployment-global destination.
///
/// The API is multi-tenant by AWS account and Region. Callers authenticate with
/// SigV4 against API Gateway (signing name execute-api). OpenAPI and boundary
/// models are generated from this model.
@service(sdkId: "ccf", arnNamespace: "execute-api")
@sigv4(name: "execute-api")
@restJson1
@title("Custom Connector Framework API")
service CustomConnectorFramework {
    version: "2025-06-01"
    operations: [
        CreateCustomConnector
        GetCustomConnector
        ListCustomConnectors
        UpdateCustomConnector
        DeleteCustomConnector
        StartCustomConnectorJob
        GetCustomConnectorJob
        StopCustomConnectorJob
        ListCustomConnectorJobs
        BatchPutCustomConnectorDocuments
        BatchDeleteCustomConnectorDocuments
        ListCustomConnectorDocuments
        PutCustomConnectorCheckpoint
        GetCustomConnectorCheckpoint
        DeleteCustomConnectorCheckpoint
    ]
}

@http(method: "POST", uri: "/api/v1/custom-connectors", code: 201)
operation CreateCustomConnector {
    input: CreateCustomConnectorRequest
    output: CreateCustomConnectorResponse
    errors: [
        BadRequestException
        UnauthorizedException
        ConflictException
        ThrottlingException
        ServiceUnavailableException
        InternalServerError
    ]
}

@readonly
@http(method: "GET", uri: "/api/v1/custom-connectors/{connector_id}", code: 200)
@tags(["runtime-callable"])
operation GetCustomConnector {
    input: GetCustomConnectorRequest
    output: GetCustomConnectorResponse
    errors: [
        BadRequestException
        UnauthorizedException
        ResourceNotFoundException
        ThrottlingException
        ServiceUnavailableException
        InternalServerError
    ]
}

@readonly
@http(method: "GET", uri: "/api/v1/custom-connectors", code: 200)
operation ListCustomConnectors {
    input: ListCustomConnectorsRequest
    output: ListCustomConnectorsResponse
    errors: [
        BadRequestException
        UnauthorizedException
        ThrottlingException
        ServiceUnavailableException
        InternalServerError
    ]
}

/// Replace supplied mutable values. Connector and job configuration are
/// replace-whole JSON values rather than provider-specific patches.
@idempotent
@http(method: "PUT", uri: "/api/v1/custom-connectors/{connector_id}", code: 200)
operation UpdateCustomConnector {
    input: UpdateCustomConnectorRequest
    output: UpdateCustomConnectorResponse
    errors: [
        BadRequestException
        UnauthorizedException
        ConflictException
        ResourceNotFoundException
        ThrottlingException
        ServiceUnavailableException
        InternalServerError
    ]
}

@idempotent
@http(method: "DELETE", uri: "/api/v1/custom-connectors/{connector_id}", code: 204)
operation DeleteCustomConnector {
    input: DeleteCustomConnectorRequest
    errors: [
        BadRequestException
        UnauthorizedException
        ConflictException
        ResourceNotFoundException
        ThrottlingException
        ServiceUnavailableException
        InternalServerError
    ]
}

@http(method: "POST", uri: "/api/v1/custom-connectors/{connector_id}/jobs", code: 201)
operation StartCustomConnectorJob {
    input: StartCustomConnectorJobRequest
    output: StartCustomConnectorJobResponse
    errors: [
        BadRequestException
        UnauthorizedException
        ConflictException
        ResourceNotFoundException
        ThrottlingException
        ServiceUnavailableException
        InternalServerError
    ]
}

/// Retrieve immutable job input and service-owned execution state.
@readonly
@http(method: "GET", uri: "/api/v1/custom-connectors/{connector_id}/jobs/{job_id}", code: 200)
@tags(["runtime-callable"])
operation GetCustomConnectorJob {
    input: GetCustomConnectorJobRequest
    output: GetCustomConnectorJobResponse
    errors: [
        BadRequestException
        UnauthorizedException
        ResourceNotFoundException
        ThrottlingException
        ServiceUnavailableException
        InternalServerError
    ]
}

@http(method: "POST", uri: "/api/v1/custom-connectors/{connector_id}/jobs/{job_id}/stop", code: 202)
operation StopCustomConnectorJob {
    input: StopCustomConnectorJobRequest
    errors: [
        BadRequestException
        UnauthorizedException
        ConflictException
        ResourceNotFoundException
        ThrottlingException
        ServiceUnavailableException
        InternalServerError
    ]
}

@readonly
@http(method: "GET", uri: "/api/v1/custom-connectors/{connector_id}/jobs", code: 200)
operation ListCustomConnectorJobs {
    input: ListCustomConnectorJobsRequest
    output: ListCustomConnectorJobsResponse
    errors: [
        BadRequestException
        UnauthorizedException
        ResourceNotFoundException
        ThrottlingException
        ServiceUnavailableException
        InternalServerError
    ]
}

@http(method: "POST", uri: "/api/v1/custom-connectors/{connector_id}/documents", code: 202)
@tags(["runtime-callable"])
operation BatchPutCustomConnectorDocuments {
    input: BatchPutCustomConnectorDocumentsRequest
    errors: [
        BadRequestException
        UnauthorizedException
        ConflictException
        ResourceNotFoundException
        ThrottlingException
        ServiceUnavailableException
        InternalServerError
    ]
}

@idempotent
@suppress(["HttpMethodSemantics.UnexpectedPayload"])
@http(method: "DELETE", uri: "/api/v1/custom-connectors/{connector_id}/documents", code: 202)
@tags(["runtime-callable"])
operation BatchDeleteCustomConnectorDocuments {
    input: BatchDeleteCustomConnectorDocumentsRequest
    errors: [
        BadRequestException
        UnauthorizedException
        ResourceNotFoundException
        ThrottlingException
        ServiceUnavailableException
        InternalServerError
    ]
}

@readonly
@http(method: "GET", uri: "/api/v1/custom-connectors/{connector_id}/documents", code: 200)
@tags(["runtime-callable"])
operation ListCustomConnectorDocuments {
    input: ListCustomConnectorDocumentsRequest
    output: ListCustomConnectorDocumentsResponse
    errors: [
        BadRequestException
        UnauthorizedException
        ResourceNotFoundException
        ThrottlingException
        ServiceUnavailableException
        InternalServerError
    ]
}

@idempotent
@http(method: "PUT", uri: "/api/v1/custom-connectors/{connector_id}/checkpoint", code: 202)
@tags(["runtime-callable"])
operation PutCustomConnectorCheckpoint {
    input: PutCustomConnectorCheckpointRequest
    errors: [
        BadRequestException
        UnauthorizedException
        ConflictException
        ResourceNotFoundException
        ThrottlingException
        ServiceUnavailableException
        InternalServerError
    ]
}

@readonly
@http(method: "GET", uri: "/api/v1/custom-connectors/{connector_id}/checkpoint", code: 200)
@tags(["runtime-callable"])
operation GetCustomConnectorCheckpoint {
    input: GetCustomConnectorCheckpointRequest
    output: GetCustomConnectorCheckpointResponse
    errors: [
        BadRequestException
        UnauthorizedException
        ResourceNotFoundException
        ThrottlingException
        ServiceUnavailableException
        InternalServerError
    ]
}

@idempotent
@http(method: "DELETE", uri: "/api/v1/custom-connectors/{connector_id}/checkpoint", code: 202)
@tags(["runtime-callable"])
operation DeleteCustomConnectorCheckpoint {
    input: DeleteCustomConnectorCheckpointRequest
    errors: [
        BadRequestException
        UnauthorizedException
        ResourceNotFoundException
        ThrottlingException
        ServiceUnavailableException
        InternalServerError
    ]
}
