import { Duration } from "aws-cdk-lib";

/**
 * Identifies the project for resource naming across the Custom Connector
 * Framework stack. Every concrete AWS resource name derives from this prefix.
 */
export enum ProjectName {
  CCF = "Ccf",
}

/**
 * Logical resource names combined with {@link ProjectName} to derive concrete
 * AWS resource names. Enum (not string literals) so no construct group inlines
 * a raw name.
 */
export enum ResourceName {
  // The customer-managed key that encrypts every table and queue, so one DisableKey call stops the service.
  SERVICE_KEY = "ServiceKey",

  // Compute: the backend Lambda handlers and framework-owned Batch identities.
  API_LAMBDA = "ApiLambda",
  SCHEDULED_START_LAMBDA = "ScheduledStartLambda",
  INGESTION_CONSUMER_LAMBDA = "IngestionConsumerLambda",

  // The queue of knowledge base ingestion requests and its dead-letter queue.
  INGESTION_QUEUE = "IngestionQueue",
  INGESTION_DLQ = "IngestionDlq",

  // Service-owned scheduling and asynchronous failure delivery.
  SCHEDULER_GROUP = "SchedulerGroup",
  SCHEDULER_TARGET_ROLE = "SchedulerTargetRole",
  SCHEDULER_TARGET_DLQ = "SchedulerTargetDlq",

  // API Gateway (the SigV4/IAM-authorized control plane).
  API = "Api",

  // Batch (runs connector containers on Fargate).
  VPC = "Vpc",
  COMPUTE_ENVIRONMENT = "BatchComputeEnvironment",
  JOB_QUEUE = "BatchJobQueue",

  // Operations: the Ops stack's CloudWatch dashboard.
  OPERATIONS_DASHBOARD = "Operations",
}

/**
 * Combines a project name with a logical PascalCase resource name to produce a
 * consistent, static resource name (tables, queues, compute, roles).
 */
export interface ResourceNameRequest {
  readonly projectName: ProjectName;
  readonly resourceName: string;
}

export const createResourceName = ({ projectName, resourceName }: ResourceNameRequest): string =>
  `${projectName}${resourceName}`;

/** The IAM path of every role and policy the framework creates, which other accounts' policies name. */
export const ROLE_PATH = "/ccf/";

/** The start of the framework's lowercase tenant resource names: destination buckets and knowledge bases. */
export const RESOURCE_PREFIX = "ccf-";

/** The start of every destination ID, which the API assigns. */
export const DESTINATION_ID_PREFIX = "dst-";

/** Every destination bucket's name starts `ccf-dst-`, so the job VPC endpoint and role boundaries can name them. */
export const DESTINATION_BUCKET_PATTERN = `${RESOURCE_PREFIX}${DESTINATION_ID_PREFIX}*`;

/** The API Lambda's role, which every destination role trusts. */
export enum ApiServiceName {
  API_ROLE = "CcfApiRole",
}

/** The ingestion consumer's role, which every destination role trusts. */
export enum IngestionQueueName {
  CONSUMER_ROLE = "CcfIngestionConsumerRole",
}

/** The connector deletion workflow's fixed names, which other accounts' policies name. */
export enum ConnectorDeletionWorkflowName {
  STATE_MACHINE = "CcfConnectorDeletionWorkflow",
  ROLE = "CcfConnectorDeletionWorkflowRole",
}

/** The job state machine's and job definitions' fixed names, which IAM grants name. */
export enum JobExecutionName {
  STATE_MACHINE = "CcfJobExecution",
  ROLE = "CcfJobExecutionRole",
  /** Every custom connector's job definition is `<prefix><destination ID>-<custom connector ID>`. */
  DEFINITION_PREFIX = "CcfJob-",
}

/** The permissions boundary of every role in the service stack. */
export const SERVICE_ROLE_BOUNDARY_NAME = "CcfServiceRoleBoundary";

/** The security group of every job container. */
export const JOB_SECURITY_GROUP_NAME = "CcfJobSecurityGroup";

/** The tag the job orchestrator puts on every Batch job it submits, which its IAM conditions require. */
export const BATCH_OWNERSHIP_TAG = {
  key: "ccf:owned-by",
  value: "custom-connector-framework",
} as const;

/** Where a job's log router writes its own logs: Batch's log group, under this stream prefix. */
export const LOG_ROUTER_LOGS = {
  logGroup: "/aws/batch/job",
  streamPrefix: "ccf-log-router",
} as const;

/** The destination workflows' waits and limits. */
export interface DestinationWorkflowSettings {
  /** The wait between checks: the knowledge base poll, both deletion waits, and the ingestion wait. */
  readonly pollInterval: Duration;
  /** The knowledge base poll limit and the ingestion check limit. */
  readonly maxPolls: number;
  /** The wait after creating the knowledge base role, so Bedrock sees it. */
  readonly rolePropagation: Duration;
  /** `DeleteDataSource`'s retry while an ingestion job runs. */
  readonly dataSourceBusyRetry: { readonly interval: Duration; readonly maxAttempts: number };
  /** How many unfinished ingestion jobs one listing reads. */
  readonly ingestionJobsPageSize: number;
  /** The quota cleanup's task timeout. */
  readonly quotaCleanupTimeout: Duration;
}

/** The ingestion queue's redrive and the consumer's limits. */
export interface IngestionQueueSettings {
  /** The visibility delay of a retried message, by receive; receives past the list keep the last delay. */
  readonly retryDelaysSeconds: readonly number[];
  /** A message goes to the dead-letter queue after this many receives. */
  readonly maxReceiveCount: number;
  /**
   * Each message assumes a tenant's destination role and starts a Bedrock ingestion job, so a burst
   * would otherwise run against STS and every tenant's Bedrock quota at once; messages wait instead.
   */
  readonly consumerReservedConcurrency: number;
}

/**
 * The service settings a deployer has no reason to change, so they aren't configuration keys.
 * The keys a deployer may change, and their defaults, are in `config/default.json`.
 */
export const SERVICE_SETTINGS = {
  /** The memory of every backend Lambda function. */
  lambdaMemoryMb: 1024,
  /** The timeout of every backend Lambda function: API Gateway's own 29-second limit. */
  lambdaTimeout: Duration.seconds(29),
  /** How long the API remembers an idempotency token and returns the first response for it. */
  idempotencyExpirySeconds: 86_400,
  /** How many times Scheduler retries a scheduled start before sending it to its dead-letter queue. */
  schedulerRetryAttempts: 3,
  /** The age after which Scheduler stops retrying a scheduled start. */
  schedulerMaxEventAgeSeconds: 3_600,
  /** The Availability Zones of the job VPC. */
  availabilityZones: 2,
  /** How long CloudFormation waits for a Batch compute environment update. */
  computeEnvironmentUpdateTimeout: Duration.minutes(30),
  /** How long every dead-letter queue keeps a message. */
  deadLetterRetention: Duration.days(14),
  /** The maximum session of every runtime role: IAM's largest, so a long job's credentials last. */
  runtimeRoleMaxSession: Duration.hours(12),
  /** How long the jobs table keeps a finished job's record. */
  jobRecordRetention: Duration.days(7),
  /**
   * The log router every job definition runs beside the connector container: AWS for Fluent Bit,
   * multi-architecture so Fargate pulls its arm64 variant. Raise it to a newer release's version tag;
   * never `latest` or a digest.
   */
  logRouterImage: "public.ecr.aws/aws-observability/aws-for-fluent-bit:2.34.3.20261102",
  /** The destination workflows' waits and limits. */
  destinationWorkflow: {
    pollInterval: Duration.seconds(30),
    maxPolls: 60,
    rolePropagation: Duration.seconds(10),
    /** 30, 60, 120, 240, then 480 seconds. */
    dataSourceBusyRetry: { interval: Duration.seconds(30), maxAttempts: 5 },
    ingestionJobsPageSize: 10,
    /** Three loops of 60 checks 30 seconds apart. */
    quotaCleanupTimeout: Duration.minutes(90),
  } satisfies DestinationWorkflowSettings,
  /** How long the `Destination` custom resource waits for the destination to become `ACTIVE` or gone. */
  destinationProviderTimeout: Duration.hours(1),
  /** The ingestion queue and its consumer. */
  ingestionQueue: {
    retryDelaysSeconds: [60, 120, 240, 480, 900],
    /** About 24 hours of retries: an ingestion job on the GitHub test destination ran 12.5 hours. */
    maxReceiveCount: 100,
    consumerReservedConcurrency: 5,
  } satisfies IngestionQueueSettings,
} as const;
