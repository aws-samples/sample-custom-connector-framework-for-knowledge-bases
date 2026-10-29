$version: "2.0"

namespace ccf

use smithy.api#sensitive

@length(min: 1, max: 64)
@pattern("^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
string ConnectorName

@length(min: 1, max: 128)
@pattern("^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
string ResourceId

@length(min: 1, max: 2048)
@pattern("^arn:[A-Za-z0-9-]+:[A-Za-z0-9-]+:[A-Za-z0-9-]*:[A-Za-z0-9-]*:.+$")
string Arn

@length(min: 1, max: 2048)
@pattern("^arn:(aws|aws-us-gov|aws-cn|aws-eusc):iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_-]+(/[A-Za-z0-9+=,.@_-]+)*$")
string RoleArn

@length(min: 12, max: 12)
@pattern("^[0-9]{12}$")
string AccountId

@length(min: 1, max: 64)
@pattern("^(?:[a-z]{2}|eusc)(?:-[a-z]+)+-[0-9]+$")
string Region

@length(min: 1, max: 2048)
@pattern("^https://[^\\s]+$")
string HttpsUri

@length(min: 1, max: 2048)
@pattern("^[0-9]{12}\\.dkr\\.ecr\\.(cn(?:-[a-z]+)+-[0-9]+\\.amazonaws\\.com\\.cn|(?:[a-bd-z][a-z]|c[a-mo-z])(?:-[a-z]+)+-[0-9]+\\.amazonaws\\.com)/[a-z0-9]+([._-][a-z0-9]+)*(/[a-z0-9]+([._-][a-z0-9]+)*)*(:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}|@sha256:[a-f0-9]{64})$")
string ImageUri

@length(min: 1, max: 1024)
string Description

@length(min: 1, max: 1024)
string CommandArgument

@length(min: 1, max: 100)
list ConnectorCommand {
    member: CommandArgument
}

@length(min: 1, max: 256)
string ScheduleExpression

@length(min: 1, max: 256)
string ScheduleTimezone

@length(min: 2, max: 1224)
@pattern("^[A-Za-z0-9_+=,.@:/-]+$")
string ExternalId

/// JSON serialized by the connector's typed configuration model. Smithy caps
/// this value at 65,536 characters. Task 6 must separately enforce that its
/// UTF-8 encoding is at most exactly 65,536 bytes; characters are not bytes.
@length(min: 2, max: 65536)
@sensitive
string ConnectorConfiguration

/// Immutable JSON input for one connector job. Smithy caps this value at
/// 65,536 characters. Task 6 must separately enforce the exact 65,536-byte
/// UTF-8 limit before persistence.
@length(min: 2, max: 65536)
@sensitive
string JobConfiguration

/// Opaque JSON checkpoint state. Runtime byte-size validation remains a
/// persistence-boundary responsibility.
@length(min: 2, max: 65536)
@sensitive
string CheckpointData

@length(min: 1, max: 2048)
string PaginationToken

@range(min: 1, max: 100)
integer MaxResults

@range(min: 0.25, max: 16.0)
float CpuUnits

@range(min: 512, max: 122880)
integer MemoryMib

@range(min: 60, max: 604800)
integer TimeoutSeconds

@length(min: 1, max: 128)
@pattern("^[A-Za-z0-9+/=_:.-]+$")
string Checksum

@length(min: 1, max: 1024)
string ObjectKey

@length(min: 1, max: 256)
string ServicePrincipal

@length(min: 1, max: 256)
string ServiceExecutionId

@length(min: 1, max: 128)
string RequestId

@length(min: 1, max: 1024)
string SanitizedErrorMessage

enum ConnectorStatus {
    AVAILABLE
    IN_USE
}

enum JobStatus {
    STARTED
    RUNNING
    STOPPING
    STOPPED
    FAILED
    SUCCEEDED
}

enum ScheduleState {
    ENABLED
    DISABLED
}

enum DestinationMode {
    S3
    BEDROCK
    QUICK
}

enum ErrorCode {
    BAD_REQUEST
    UNAUTHORIZED
    RESOURCE_NOT_FOUND
    CONFLICT
    RESOURCE_LIMIT_EXCEEDED
    THROTTLED
    INTERNAL_SERVER_ERROR
    SERVICE_UNAVAILABLE
}

structure ResourceRequirements {
    /// vCPU units allocated to the framework-owned job.
    @required
    cpu: CpuUnits

    /// Memory allocated to the framework-owned job, in MiB.
    @required
    memory: MemoryMib
}

structure ContainerProperties {
    @required
    image_uri: ImageUri

    /// Command executed by the framework-owned job runner.
    @required
    command: ConnectorCommand

    @required
    resource_requirements: ResourceRequirements

    /// Maximum job runtime in seconds.
    @required
    timeout: TimeoutSeconds
}

/// Only provided fields are replaced. The service owns execution and job roles.
structure UpdateContainerProperties {
    image_uri: ImageUri
    command: ConnectorCommand
    resource_requirements: ResourceRequirements
    timeout: TimeoutSeconds
}

structure SourceAccessConfiguration {
    /// Exact client-account role assumed by the framework job.
    @required
    role_arn: RoleArn

    /// Connector-specific external ID required by the role trust policy.
    @required
    external_id: ExternalId
}

structure ConnectorSchedule {
    @required
    expression: ScheduleExpression

    @required
    state: ScheduleState

    timezone: ScheduleTimezone
}

/// Deployment-global destination selected when CCF is deployed. Connectors do
/// not choose a destination.
structure DeploymentDestination {
    @required
    mode: DestinationMode

    @required
    bucket_arn: Arn

    object_prefix: ObjectKey
}

/// Typed cross-account reference supplied by the CCF deployment.
structure CcfServiceReference {
    @required
    endpoint: HttpsUri

    @required
    region: Region

    @required
    service_principal: ServicePrincipal

    @required
    service_account_id: AccountId

    @required
    destination: DeploymentDestination
}

/// Connector view without sensitive configuration, returned by create/update/list.
structure CustomConnectorSummary {
    @required
    connector_id: ResourceId

    @required
    arn: Arn

    @required
    name: ConnectorName

    @required
    created_at: Timestamp

    @required
    updated_at: Timestamp

    @required
    status: ConnectorStatus

    description: Description
}

structure CustomConnectorDetail {
    @required
    connector_id: ResourceId

    @required
    arn: Arn

    @required
    name: ConnectorName

    @required
    created_at: Timestamp

    @required
    updated_at: Timestamp

    @required
    status: ConnectorStatus

    description: Description

    @required
    container_properties: ContainerProperties

    /// Replace-whole, provider-neutral JSON. Provider schemas may include lists
    /// such as multiple repositories without adding provider-specific operations.
    @required
    connector_configuration: ConnectorConfiguration

    @required
    source_access: SourceAccessConfiguration

    schedule: ConnectorSchedule
}

@length(min: 0, max: 100)
list CustomConnectorSummaryList {
    member: CustomConnectorSummary
}

/// Framework execution values are service-authored and never accepted from a
/// connector create or update request.
structure ServiceExecution {
    @required
    execution_id: ServiceExecutionId

    @required
    execution_role_arn: RoleArn

    @required
    job_role_arn: RoleArn

    started_at: Timestamp
    completed_at: Timestamp
}

structure JobSummary {
    @required
    job_id: ResourceId

    @required
    connector_id: ResourceId

    @required
    status: JobStatus

    @required
    created_at: Timestamp

    @required
    updated_at: Timestamp
}

structure JobDetail {
    @required
    job_id: ResourceId

    @required
    connector_id: ResourceId

    @required
    status: JobStatus

    @required
    job_configuration: JobConfiguration

    @required
    created_at: Timestamp

    @required
    updated_at: Timestamp

    /// Absent until the framework has submitted the service-owned execution.
    service_execution: ServiceExecution
}

@length(min: 0, max: 100)
list JobSummaryList {
    member: JobSummary
}

structure DocumentItem {
    @required
    document_id: ResourceId

    /// Content checksum used for change detection.
    @required
    checksum: Checksum

    /// Object written to the deployment-global destination.
    @required
    object_key: ObjectKey

    /// Optional provider metadata sidecar. At most one sidecar is supported.
    sidecar_key: ObjectKey
}

@length(min: 1, max: 100)
list DocumentItemList {
    member: DocumentItem
}

@length(min: 1, max: 100)
list DocumentIdList {
    member: ResourceId
}

structure DocumentSummary {
    @required
    document_id: ResourceId

    @required
    checksum: Checksum

    @required
    object_key: ObjectKey

    sidecar_key: ObjectKey

    @required
    created_at: Timestamp

    @required
    updated_at: Timestamp
}

@length(min: 0, max: 100)
list DocumentSummaryList {
    member: DocumentSummary
}

structure Checkpoint {
    @required
    connector_id: ResourceId

    @required
    checkpoint_data: CheckpointData

    @required
    created_at: Timestamp

    @required
    updated_at: Timestamp
}

structure CreateCustomConnectorRequest {
    @required
    name: ConnectorName

    description: Description

    @required
    container_properties: ContainerProperties

    @required
    connector_configuration: ConnectorConfiguration

    @required
    source_access: SourceAccessConfiguration

    schedule: ConnectorSchedule
}

structure CreateCustomConnectorResponse {
    @required
    connector: CustomConnectorSummary
}

structure GetCustomConnectorRequest {
    @required
    @httpLabel
    connector_id: ResourceId
}

structure GetCustomConnectorResponse {
    @required
    connector: CustomConnectorDetail

    @required
    service_reference: CcfServiceReference
}

structure ListCustomConnectorsRequest {
    @httpQuery("max_results")
    max_results: MaxResults

    @httpQuery("next_token")
    next_token: PaginationToken
}

structure ListCustomConnectorsResponse {
    @required
    connectors: CustomConnectorSummaryList

    next_token: PaginationToken
}

structure UpdateCustomConnectorRequest {
    @required
    @httpLabel
    connector_id: ResourceId

    name: ConnectorName
    description: Description
    container_properties: UpdateContainerProperties

    /// Replaces the complete global configuration; partial patches are not supported.
    connector_configuration: ConnectorConfiguration
    source_access: SourceAccessConfiguration
    schedule: ConnectorSchedule
}

structure UpdateCustomConnectorResponse {
    @required
    connector: CustomConnectorSummary
}

structure DeleteCustomConnectorRequest {
    @required
    @httpLabel
    connector_id: ResourceId
}

structure StartCustomConnectorJobRequest {
    @required
    @httpLabel
    connector_id: ResourceId

    /// Immutable replace-whole JSON input for this job.
    @required
    job_configuration: JobConfiguration
}

structure StartCustomConnectorJobResponse {
    @required
    job: JobSummary
}

structure GetCustomConnectorJobRequest {
    @required
    @httpLabel
    connector_id: ResourceId

    @required
    @httpLabel
    job_id: ResourceId
}

structure GetCustomConnectorJobResponse {
    @required
    job: JobDetail
}

structure StopCustomConnectorJobRequest {
    @required
    @httpLabel
    connector_id: ResourceId

    @required
    @httpLabel
    job_id: ResourceId
}

structure ListCustomConnectorJobsRequest {
    @required
    @httpLabel
    connector_id: ResourceId

    @httpQuery("max_results")
    max_results: MaxResults

    @httpQuery("next_token")
    next_token: PaginationToken

    @httpQuery("status")
    status: JobStatus
}

structure ListCustomConnectorJobsResponse {
    @required
    jobs: JobSummaryList

    next_token: PaginationToken
}

structure BatchPutCustomConnectorDocumentsRequest {
    @required
    @httpLabel
    connector_id: ResourceId

    @required
    documents: DocumentItemList
}

structure BatchDeleteCustomConnectorDocumentsRequest {
    @required
    @httpLabel
    connector_id: ResourceId

    @required
    document_ids: DocumentIdList
}

structure ListCustomConnectorDocumentsRequest {
    @required
    @httpLabel
    connector_id: ResourceId

    @httpQuery("max_results")
    max_results: MaxResults

    @httpQuery("next_token")
    next_token: PaginationToken
}

structure ListCustomConnectorDocumentsResponse {
    @required
    documents: DocumentSummaryList

    next_token: PaginationToken
}

structure PutCustomConnectorCheckpointRequest {
    @required
    @httpLabel
    connector_id: ResourceId

    @required
    checkpoint_data: CheckpointData
}

structure GetCustomConnectorCheckpointRequest {
    @required
    @httpLabel
    connector_id: ResourceId
}

structure GetCustomConnectorCheckpointResponse {
    @required
    checkpoint: Checkpoint
}

structure DeleteCustomConnectorCheckpointRequest {
    @required
    @httpLabel
    connector_id: ResourceId
}
