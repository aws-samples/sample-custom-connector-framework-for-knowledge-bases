$version: "2.0"

namespace ccf

@pattern("^dst-[0-9a-f]{12}$")
string DestinationId

@pattern("^cc-[0-9a-f]{12}$")
string ConnectorId

@pattern("^ccj-([0-9a-f]{12}|[0-9a-f]{32})$")
string JobId

/// col-default, or col- plus the first 16 hex characters of the SHA-256 of the source collection ID.
@pattern("^(col-default|col-[0-9a-f]{16})$")
string CollectionId

/// doc- plus the first 16 hex characters of the SHA-256 of the source document ID.
@pattern("^doc-[0-9a-f]{16}$")
string DocumentId

@length(min: 1, max: 1024)
string SourceCollectionId

@length(min: 1, max: 1024)
string SourceDocumentId

@length(min: 1, max: 64)
@pattern("^[A-Za-z0-9._-]{1,64}$")
string ResourceName

@length(min: 1, max: 64)
string ClientToken

@length(min: 1, max: 2048)
@pattern("^arn:[A-Za-z0-9-]+:[A-Za-z0-9-]+:[A-Za-z0-9-]*:[A-Za-z0-9-]*:.+$")
string Arn

@length(min: 1, max: 2048)
@pattern("^arn:(aws|aws-us-gov|aws-cn|aws-eusc):iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_-]+(/[A-Za-z0-9+=,.@_-]+)*$")
string RoleArn

/// A destination role: role/ccf/destinations/CcfDestinationRole-<name> in any partition.
@pattern("^arn:(aws|aws-us-gov|aws-cn|aws-eusc):iam::[0-9]{12}:role/ccf/destinations/CcfDestinationRole-[A-Za-z0-9._-]{1,45}$")
string DestinationRoleArn

/// A role in the caller's account named CcfSourceRole-<name>, with no path.
@length(min: 1, max: 2048)
@pattern("^arn:(aws|aws-us-gov|aws-cn|aws-eusc):iam::[0-9]{12}:role/CcfSourceRole-[A-Za-z0-9+=,.@_-]{1,50}$")
string SourceRoleArn

/// A role in the caller's account named CcfLogDeliveryRole-<name>, with no path.
@length(min: 1, max: 2048)
@pattern("^arn:(aws|aws-us-gov|aws-cn|aws-eusc):iam::[0-9]{12}:role/CcfLogDeliveryRole-[A-Za-z0-9+=,.@_-]{1,45}$")
string LogDeliveryRoleArn

@length(min: 1, max: 2048)
@pattern("^arn:(aws|aws-us-gov|aws-cn|aws-eusc):secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:[A-Za-z0-9/_+=.@-]+$")
string SecretArn

/// A CloudWatch Logs log group ARN, optionally ending in :*.
@length(min: 1, max: 2048)
@pattern("^arn:(aws|aws-us-gov|aws-cn|aws-eusc):logs:[a-z0-9-]+:[0-9]{12}:log-group:[.\\-_/#A-Za-z0-9]{1,512}(:\\*)?$")
string LogGroupArn

/// An image in an ECR repository under ccf-connectors/, tagged or pinned by digest, in any partition:
/// amazonaws.com, amazonaws.com.cn for aws-cn, or amazonaws.eu for aws-eusc.
@length(min: 1, max: 2048)
@pattern("^[0-9]{12}\\.dkr\\.ecr\\.(cn(?:-[a-z]+)+-[0-9]+\\.amazonaws\\.com\\.cn|eusc(?:-[a-z]+)+-[0-9]+\\.amazonaws\\.eu|(?:[a-bd-z][a-z]|c[a-mo-z])(?:-[a-z]+)+-[0-9]+\\.amazonaws\\.com)/ccf-connectors/[a-z0-9]+([._/-][a-z0-9]+)*(:[A-Za-z0-9_][A-Za-z0-9._-]{0,127}|@sha256:[a-f0-9]{64})$")
string ImageUri

@pattern("^[0-9A-Za-z]{10}$")
string DataSourceId

@length(min: 1, max: 1024)
string Description

@length(min: 1, max: 2048)
string StatusReason

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

/// Opaque to CCF: never parsed. CCF also rejects a value over 65,536 UTF-8 bytes.
@length(max: 65536)
@sensitive
string ConnectorConfiguration

/// Opaque to CCF: never parsed. CCF also rejects a value over 65,536 UTF-8 bytes.
@length(max: 65536)
@sensitive
string JobConfiguration

/// Opaque to CCF: never parsed. CCF also rejects a value over 16,384 UTF-8 bytes.
@length(min: 2, max: 16384)
@sensitive
string CheckpointData

@length(min: 1, max: 2048)
string PaginationToken

@range(min: 1, max: 100)
integer MaxResults

/// At least 0.5, because the log router takes 0.25 vCPU of the job's task size.
@range(min: 0.5, max: 16.0)
float CpuUnits

/// At least 1,024, because the log router takes 512 MiB of the job's task size.
@range(min: 1024, max: 122880)
integer MemoryMib

@range(min: 60, max: 604800)
integer TimeoutSeconds

@length(min: 1, max: 128)
@pattern("^[A-Za-z0-9+/=_:.-]+$")
string ContentHash

@length(min: 1, max: 1024)
string SourceVersion

@length(min: 1, max: 256)
string ServiceExecutionId

@length(min: 1, max: 128)
string RequestId

@length(min: 1, max: 1024)
string SanitizedErrorMessage

@pattern("^[A-Za-z0-9._:-]{1,64}$")
string CorrelationId

/// arn:<partition>:ccf:<Region>:<account ID>, the prefix of every CCF ARN a tenant owns.
@length(min: 1, max: 2048)
@pattern("^arn:(aws|aws-us-gov|aws-cn|aws-eusc):ccf:(?:[a-z]{2}|eusc)(?:-[a-z]+)+-[0-9]+:[0-9]{12}$")
string TenantArnPrefix

@length(min: 3, max: 63)
@pattern("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
string BucketName

@pattern("^[0-9A-Za-z]{10}$")
string KnowledgeBaseId

enum DestinationType {
    BEDROCK
    QUICK
    S3
}

enum DestinationStatus {
    CREATING_BUCKET
    CREATING_KNOWLEDGE_BASE
    CREATING_DATA_SOURCE
    ACTIVE
    FAILED
    DELETING
}

/// A metadata field's name, as documents' metadata files spell it. A leading underscore is reserved by Bedrock.
@length(min: 1, max: 200)
@pattern("^[A-Za-z0-9][A-Za-z0-9_-]*$")
string MetadataFieldName

/// The value a metadata field holds: Bedrock declares only these two types.
enum MetadataFieldType {
    STRING
    STRING_LIST
}

/// Whether the knowledge base makes the field searchable, Bedrock's `searchability` setting.
enum MetadataFieldSearchability {
    ENABLED
    DISABLED
}

/// One custom metadata field of a BEDROCK destination's knowledge base.
structure MetadataField {
    @required
    name: MetadataFieldName

    @required
    type: MetadataFieldType

    @required
    searchability: MetadataFieldSearchability
}

/// Up to 30 fields, the knowledge base's limit, each name once.
@length(min: 0, max: 30)
list MetadataFieldList {
    member: MetadataField
}

enum ConnectorStatus {
    AVAILABLE
    IN_USE
    DELETING
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

enum LogDestinationType {
    CLOUDWATCH_LOGS
}

enum ErrorCode {
    BAD_REQUEST
    UNAUTHORIZED
    RESOURCE_NOT_FOUND
    CONFLICT
    THROTTLED
    INTERNAL_SERVER_ERROR
    SERVICE_UNAVAILABLE
}

/// Each member is set once its resource exists.
structure DestinationResources {
    bucket_arn: Arn
    knowledge_base_arn: Arn
    data_source_id: DataSourceId
    knowledge_base_role_arn: RoleArn
    bucket_name: BucketName
    knowledge_base_id: KnowledgeBaseId
}

// The headers every operation accepts.
@mixin
structure CallerHeaders {
    /// Ties together the request's log lines; the response returns it. CCF creates one when it's left out.
    @httpHeader("x-correlation-id")
    correlation_id: CorrelationId

    /// Sent only by a custom connector's runtime role, to name the tenant it acts for.
    @httpHeader("x-ccf-tenant-arn-prefix")
    tenant_arn_prefix: TenantArnPrefix
}

structure DestinationSummary {
    @required
    destination_id: DestinationId

    @required
    arn: Arn

    @required
    name: ResourceName

    @required
    type: DestinationType

    @required
    status: DestinationStatus

    @required
    created_at: Timestamp

    @required
    updated_at: Timestamp
}

/// The destination as CreateDestination returns it, before its workflow runs.
structure CreatedDestination {
    @required
    destination_id: DestinationId

    @required
    arn: Arn

    @required
    name: ResourceName

    description: Description

    @required
    type: DestinationType

    @required
    access_control_enabled: Boolean

    @required
    status: DestinationStatus

    @required
    created_at: Timestamp

    @required
    updated_at: Timestamp
}

structure Destination {
    @required
    destination_id: DestinationId

    @required
    arn: Arn

    @required
    name: ResourceName

    description: Description

    @required
    type: DestinationType

    @required
    access_control_enabled: Boolean

    @required
    status: DestinationStatus

    status_reason: StatusReason

    /// The tenant's destination role, which CCF assumes to reach the destination's resources.
    @required
    role_arn: RoleArn

    @required
    destination_resources: DestinationResources

    /// The knowledge base's custom metadata fields, which UpdateDestination sets: empty until then, and for S3 and
    /// QUICK.
    metadata_fields: MetadataFieldList = []

    @required
    created_at: Timestamp

    @required
    updated_at: Timestamp
}

@length(min: 0, max: 100)
list DestinationSummaryList {
    member: DestinationSummary
}

structure ResourceRequirements {
    /// vCPU for the job's task. The connector container gets this minus 0.25 vCPU for the log router.
    @required
    cpu: CpuUnits

    /// Memory for the job's task, in MiB. The connector container gets this minus 512 MiB for the log router.
    @required
    memory: MemoryMib
}

structure ContainerProperties {
    @required
    image_uri: ImageUri

    @required
    command: ConnectorCommand

    @required
    resource_requirements: ResourceRequirements

    /// Maximum job run time, in seconds.
    @required
    timeout: TimeoutSeconds
}

structure UpdateContainerProperties {
    image_uri: ImageUri
    command: ConnectorCommand
    resource_requirements: ResourceRequirements
    timeout: TimeoutSeconds
}

/// The source-access role and secret a caller registers.
structure SourceAccessInput {
    @required
    role_arn: SourceRoleArn

    /// A Secrets Manager secret in the caller's account and the service Region.
    secret_arn: SecretArn
}

structure SourceAccess {
    @required
    role_arn: SourceRoleArn

    secret_arn: SecretArn

    /// Generated by CCF. The source-access role's trust policy requires it.
    @required
    external_id: ExternalId
}

/// Where the connector container's log lines go, in the caller's account.
structure LogDestination {
    @required
    type: LogDestinationType

    /// A log group in the caller's account and the service Region.
    @required
    arn: LogGroupArn

    @required
    role_arn: LogDeliveryRoleArn
}

structure ConnectorSchedule {
    @required
    expression: ScheduleExpression

    @required
    state: ScheduleState

    timezone: ScheduleTimezone
}

structure ConnectorSummary {
    @required
    connector_id: ConnectorId

    @required
    arn: Arn

    @required
    destination_id: DestinationId

    @required
    name: ResourceName

    description: Description

    @required
    status: ConnectorStatus

    @required
    created_at: Timestamp

    @required
    updated_at: Timestamp
}

/// The custom connector as CreateCustomConnector returns it.
structure CreatedConnector {
    @required
    connector_id: ConnectorId

    @required
    arn: Arn

    @required
    destination_id: DestinationId

    @required
    name: ResourceName

    description: Description

    @required
    status: ConnectorStatus

    @required
    created_at: Timestamp

    @required
    updated_at: Timestamp

    /// The role the custom connector's jobs run as. Name it as the source-access role's trust principal.
    @required
    runtime_role_arn: RoleArn

    /// The external ID the source-access role's trust policy requires.
    @required
    source_access: SourceAccess
}

structure Connector {
    @required
    connector_id: ConnectorId

    @required
    arn: Arn

    @required
    destination_id: DestinationId

    @required
    name: ResourceName

    description: Description

    @required
    status: ConnectorStatus

    @required
    created_at: Timestamp

    @required
    updated_at: Timestamp

    /// The role the custom connector's jobs run as. Name it as the source-access role's trust principal.
    @required
    runtime_role_arn: RoleArn

    @required
    container_properties: ContainerProperties

    connector_configuration: ConnectorConfiguration

    @required
    source_access: SourceAccess

    log_destination: LogDestination

    schedule: ConnectorSchedule
}

@length(min: 0, max: 100)
list ConnectorSummaryList {
    member: ConnectorSummary
}

/// Set by CCF from the AWS Batch job when the job ends. A job stopped through the API has none.
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
    job_id: JobId

    @required
    arn: Arn

    @required
    status: JobStatus

    @required
    created_at: Timestamp

    @required
    updated_at: Timestamp
}

structure Job {
    @required
    job_id: JobId

    @required
    arn: Arn

    @required
    status: JobStatus

    @required
    created_at: Timestamp

    @required
    updated_at: Timestamp

    status_reason: StatusReason

    job_configuration: JobConfiguration

    service_execution: ServiceExecution
}

@length(min: 0, max: 100)
list JobSummaryList {
    member: JobSummary
}

/// A document whose file and metadata file the caller already wrote to their final keys.
structure DocumentRecord {
    @required
    document_id: DocumentId

    @required
    source_document_id: SourceDocumentId

    /// The content's hash, compared to skip unchanged documents.
    @required
    content_hash: ContentHash

    source_version: SourceVersion
}

@length(min: 1, max: 99)
list DocumentRecordList {
    member: DocumentRecord
}

@length(min: 1, max: 99)
@uniqueItems
list DocumentIdList {
    member: DocumentId
}

structure StoredDocument {
    @required
    collection_id: CollectionId

    @required
    document_id: DocumentId

    @required
    source_document_id: SourceDocumentId

    @required
    content_hash: ContentHash

    source_version: SourceVersion

    @required
    created_at: Timestamp

    @required
    updated_at: Timestamp
}

@length(min: 0, max: 100)
list StoredDocumentList {
    member: StoredDocument
}

structure CollectionReference {
    @required
    collection_id: CollectionId
}

@length(min: 1, max: 100)
@uniqueItems
list CollectionReferenceList {
    member: CollectionReference
}

structure Collection {
    @required
    collection_id: CollectionId

    /// Absent for col-default.
    source_collection_id: SourceCollectionId

    checkpoint_data: CheckpointData

    @required
    created_at: Timestamp

    @required
    updated_at: Timestamp
}

@length(min: 0, max: 100)
list CollectionList {
    member: Collection
}

structure Checkpoint {
    @required
    checkpoint_data: CheckpointData

    @required
    created_at: Timestamp

    @required
    updated_at: Timestamp
}

structure CreateDestinationRequest with [CallerHeaders] {
    @required
    name: ResourceName

    description: Description

    @required
    type: DestinationType

    /// The caller account's destination role.
    @required
    role_arn: DestinationRoleArn

    /// Turns on access control lists in the data source. Only BEDROCK accepts true.
    access_control_enabled: Boolean = false

    @idempotencyToken
    client_token: ClientToken
}

structure CreateDestinationResponse {
    @required
    destination: CreatedDestination
}

structure GetDestinationRequest with [CallerHeaders] {
    @required
    @httpLabel
    destination_id: DestinationId

    /// Sent by a runtime role to name the custom connector it runs for.
    @httpHeader("x-ccf-connector-id")
    connector_id: ConnectorId
}

structure GetDestinationResponse {
    @required
    destination: Destination
}

structure ListDestinationsRequest with [CallerHeaders] {
    /// Return only the destinations with exactly this name.
    @httpQuery("name")
    name: ResourceName

    @httpQuery("max_results")
    max_results: MaxResults = 100

    @httpQuery("next_token")
    next_token: PaginationToken
}

structure ListDestinationsResponse {
    @required
    destinations: DestinationSummaryList

    next_token: PaginationToken
}

structure DeleteDestinationRequest with [CallerHeaders] {
    @required
    @httpLabel
    destination_id: DestinationId
}

structure UpdateDestinationRequest with [CallerHeaders] {
    @required
    @httpLabel
    destination_id: DestinationId

    /// Replaces the knowledge base's custom metadata fields; an empty list removes them all.
    @required
    metadata_fields: MetadataFieldList
}

structure UpdateDestinationResponse {
    @required
    destination: Destination
}

structure CreateCustomConnectorRequest with [CallerHeaders] {
    @required
    @httpLabel
    destination_id: DestinationId

    @required
    name: ResourceName

    description: Description

    @required
    container_properties: ContainerProperties

    connector_configuration: ConnectorConfiguration

    @required
    source_access: SourceAccessInput

    log_destination: LogDestination

    schedule: ConnectorSchedule

    @idempotencyToken
    client_token: ClientToken
}

structure CreateCustomConnectorResponse {
    @required
    connector: CreatedConnector
}

structure GetCustomConnectorRequest with [CallerHeaders] {
    @required
    @httpLabel
    destination_id: DestinationId

    @required
    @httpLabel
    connector_id: ConnectorId
}

structure GetCustomConnectorResponse {
    @required
    connector: Connector
}

structure ListCustomConnectorsRequest with [CallerHeaders] {
    @required
    @httpLabel
    destination_id: DestinationId

    /// Return only the custom connectors with exactly this name.
    @httpQuery("name")
    name: ResourceName

    @httpQuery("max_results")
    max_results: MaxResults = 100

    @httpQuery("next_token")
    next_token: PaginationToken
}

structure ListCustomConnectorsResponse {
    @required
    connectors: ConnectorSummaryList

    next_token: PaginationToken
}

/// An update member that null clears and omission keeps. The OpenAPI document lets the member
/// be null; `codegen/enrich_openapi.py` reads this trait.
@trait(selector: "structure > member")
structure clearable {}

structure UpdateCustomConnectorRequest with [CallerHeaders] {
    @required
    @httpLabel
    destination_id: DestinationId

    @required
    @httpLabel
    connector_id: ConnectorId

    name: ResourceName

    description: Description

    container_properties: UpdateContainerProperties

    connector_configuration: ConnectorConfiguration

    source_access: SourceAccessInput

    /// Null removes the log destination; left out, it is kept.
    @clearable
    log_destination: LogDestination

    /// Null deletes the schedule; left out, it is kept.
    @clearable
    schedule: ConnectorSchedule
}

structure UpdateCustomConnectorResponse {
    @required
    connector: ConnectorSummary
}

structure DeleteCustomConnectorRequest with [CallerHeaders] {
    @required
    @httpLabel
    destination_id: DestinationId

    @required
    @httpLabel
    connector_id: ConnectorId
}

structure StartCustomConnectorJobRequest with [CallerHeaders] {
    @required
    @httpLabel
    destination_id: DestinationId

    @required
    @httpLabel
    connector_id: ConnectorId

    job_configuration: JobConfiguration

    @idempotencyToken
    client_token: ClientToken
}

structure StartCustomConnectorJobResponse {
    @required
    job: JobSummary
}

structure GetCustomConnectorJobRequest with [CallerHeaders] {
    @required
    @httpLabel
    destination_id: DestinationId

    @required
    @httpLabel
    connector_id: ConnectorId

    @required
    @httpLabel
    job_id: JobId
}

structure GetCustomConnectorJobResponse {
    @required
    job: Job
}

structure ListCustomConnectorJobsRequest with [CallerHeaders] {
    @required
    @httpLabel
    destination_id: DestinationId

    @required
    @httpLabel
    connector_id: ConnectorId

    @httpQuery("status")
    status: JobStatus

    @httpQuery("max_results")
    max_results: MaxResults = 100

    @httpQuery("next_token")
    next_token: PaginationToken
}

/// Jobs are newest first.
structure ListCustomConnectorJobsResponse {
    @required
    jobs: JobSummaryList

    next_token: PaginationToken
}

structure StopCustomConnectorJobRequest with [CallerHeaders] {
    @required
    @httpLabel
    destination_id: DestinationId

    @required
    @httpLabel
    connector_id: ConnectorId

    @required
    @httpLabel
    job_id: JobId
}

structure BatchPutCustomConnectorDocumentsRequest with [CallerHeaders] {
    @required
    @httpLabel
    destination_id: DestinationId

    @required
    @httpLabel
    connector_id: ConnectorId

    collection_id: CollectionId = "col-default"

    /// Required for every collection except col-default, which takes none.
    source_collection_id: SourceCollectionId

    @required
    documents: DocumentRecordList
}

structure BatchDeleteCustomConnectorDocumentsRequest with [CallerHeaders] {
    @required
    @httpLabel
    destination_id: DestinationId

    @required
    @httpLabel
    connector_id: ConnectorId

    collection_id: CollectionId = "col-default"

    @required
    document_ids: DocumentIdList
}

structure ListCustomConnectorDocumentsRequest with [CallerHeaders] {
    @required
    @httpLabel
    destination_id: DestinationId

    @required
    @httpLabel
    connector_id: ConnectorId

    /// Return only the documents in this collection.
    @httpQuery("collection_id")
    collection_id: CollectionId

    @httpQuery("max_results")
    max_results: MaxResults = 100

    @httpQuery("next_token")
    next_token: PaginationToken
}

structure ListCustomConnectorDocumentsResponse {
    @required
    documents: StoredDocumentList

    next_token: PaginationToken
}

structure ListCustomConnectorCollectionsRequest with [CallerHeaders] {
    @required
    @httpLabel
    destination_id: DestinationId

    @required
    @httpLabel
    connector_id: ConnectorId

    /// Return only this collection.
    @httpQuery("collection_id")
    collection_id: CollectionId

    @httpQuery("max_results")
    max_results: MaxResults = 100

    @httpQuery("next_token")
    next_token: PaginationToken
}

structure ListCustomConnectorCollectionsResponse {
    @required
    collections: CollectionList

    next_token: PaginationToken
}

structure BatchDeleteCustomConnectorCollectionsRequest with [CallerHeaders] {
    @required
    @httpLabel
    destination_id: DestinationId

    @required
    @httpLabel
    connector_id: ConnectorId

    @required
    collections: CollectionReferenceList
}

structure PutCustomConnectorCheckpointRequest with [CallerHeaders] {
    @required
    @httpLabel
    destination_id: DestinationId

    @required
    @httpLabel
    connector_id: ConnectorId

    collection_id: CollectionId = "col-default"

    /// Required for every collection except col-default, which takes none.
    source_collection_id: SourceCollectionId

    @required
    checkpoint_data: CheckpointData
}

structure GetCustomConnectorCheckpointRequest with [CallerHeaders] {
    @required
    @httpLabel
    destination_id: DestinationId

    @required
    @httpLabel
    connector_id: ConnectorId

    @httpQuery("collection_id")
    collection_id: CollectionId = "col-default"
}

structure GetCustomConnectorCheckpointResponse {
    /// Absent when no checkpoint is stored.
    checkpoint: Checkpoint
}

structure DeleteCustomConnectorCheckpointRequest with [CallerHeaders] {
    @required
    @httpLabel
    destination_id: DestinationId

    @required
    @httpLabel
    connector_id: ConnectorId

    @httpQuery("collection_id")
    collection_id: CollectionId = "col-default"
}
