import { RemovalPolicy } from "aws-cdk-lib";

/**
 * Identifies the project for resource naming across the Custom Connector
 * Framework stack. Every concrete AWS resource name derives from this prefix.
 */
export enum ProjectName {
  CCF = "Ccf",
}

/**
 * Deployment environment, chosen with `--context env=dev|prod`. Defaulting to
 * dev keeps a bare `cdk synth` from ever targeting production.
 */
export enum EnvironmentName {
  DEV = "Dev",
  PROD = "Prod",
}

/**
 * The knowledge base provider the framework lands documents for. Selected with
 * `--context provider=s3|bedrock|quick`.
 *
 * - `S3` provisions only the S3 ingestion bucket and the state/orchestration
 *   backend. Documents are stored in S3 as-is, with no managed-KB metadata
 *   sidecar and no per-document ACL file; access is governed by the bucket's
 *   IAM and bucket policy. This is the default.
 * - `BEDROCK` additionally provisions a Bedrock Managed Knowledge Base on S3
 *   Vectors whose data source ingests the ingestion bucket.
 * - `QUICK` provisions only the S3 ingestion bucket; an Amazon Quick Knowledge
 *   Base is created in the Amazon Quick console against that bucket (Quick Index
 *   crawls S3 on its own schedule, so there is no CDK-managed KB resource).
 */
export enum Provider {
  S3 = "s3",
  BEDROCK = "bedrock",
  QUICK = "quick",
}

/**
 * Logical resource names combined with {@link ProjectName} to derive concrete
 * AWS resource names. Enum (not string literals) so no construct group inlines
 * a raw name (cdk-best-practices rule 5).
 */
export enum ResourceName {
  // DynamoDB tables (the state service).
  CONNECTORS_TABLE = "CustomConnectors",
  JOBS_TABLE = "CustomConnectorJobs",
  DOCUMENTS_TABLE = "CustomConnectorDocuments",

  // The single customer-managed key encrypting every table, the bucket, and the
  // vector store, so one DisableKey call is the service's andon cord.
  SERVICE_KEY = "ServiceKey",

  // S3 (purpose segment only; createBucketName suffixes account + region).
  INGESTION_BUCKET = "ingestion",
  ACCESS_LOGS_BUCKET = "access-logs",

  // Compute: the backend Lambda handlers and framework-owned Batch identities.
  API_LAMBDA = "ApiLambda",
  JOB_ORCHESTRATOR_LAMBDA = "JobOrchestratorLambda",
  JOB_STATUS_LAMBDA = "JobStatusLambda",
  SCHEDULED_START_LAMBDA = "ScheduledStartLambda",
  FRAMEWORK_EXECUTION_ROLE = "FrameworkExecutionRole",
  SOURCE_ROLE_PREFIX = "SourceRole",

  // Service-owned scheduling and asynchronous failure delivery.
  SCHEDULER_GROUP = "SchedulerGroup",
  SCHEDULER_TARGET_ROLE = "SchedulerTargetRole",
  SCHEDULER_TARGET_DLQ = "SchedulerTargetDlq",
  JOBS_STREAM_DLQ = "JobsStreamDlq",
  BATCH_STATUS_DLQ = "BatchStatusDlq",

  // API Gateway (the SigV4/IAM-authorized control plane).
  API = "Api",

  // Batch (runs connector containers on Fargate).
  VPC = "Vpc",
  COMPUTE_ENVIRONMENT = "BatchComputeEnvironment",
  JOB_QUEUE = "BatchJobQueue",

  // Routing: DynamoDB stream -> orchestrator, EventBridge Batch state -> status.
  JOBS_STREAM_MAPPING = "JobsTableStreamMapping",
  BATCH_JOB_STATUS_RULE = "BatchJobStatusRule",

  // Bedrock Managed Knowledge Base on S3 Vectors (provider=bedrock only).
  KNOWLEDGE_BASE = "KnowledgeBase",
  KNOWLEDGE_BASE_ROLE = "KnowledgeBaseRole",
  KNOWLEDGE_BASE_DATA_SOURCE = "KnowledgeBaseDataSource",
  VECTOR_BUCKET = "VectorBucket",
  VECTOR_INDEX = "VectorIndex",
}

/**
 * Combines a project name with a logical PascalCase resource name to produce a
 * consistent, static resource name (tables, queues, compute, roles).
 */
export const createResourceName = (projectName: ProjectName, resourceName: string): string =>
  `${projectName}${resourceName}`;

/**
 * Builds a globally-unique S3 bucket name from the project name, a bucket
 * purpose, the account, and the region. Lowercased here so every caller gets a
 * valid name whatever the casing of the purpose segment (S3 and S3 Vectors
 * both forbid uppercase).
 */
export const createBucketName = (
  projectName: ProjectName,
  bucketName: string,
  account: string,
  region: string,
): string => {
  const kebabProjectName = projectName
    .toString()
    .replace(/([A-Z])/g, "-$1")
    .replace(/^-/, "")
    .toLowerCase();
  return `${kebabProjectName}-${bucketName}-${account}-${region}`.toLowerCase();
};

/** Builds a stack name from the project name and a PascalCase stack identifier. */
export const createStackName = (projectName: ProjectName, stackName: string): string => {
  const pascalStackName = stackName.charAt(0).toUpperCase() + stackName.slice(1);
  return `${projectName}${pascalStackName}Stack`;
};

/** Capitalizes a resource name for use as a CDK construct id. */
export const createConstructId = (resourceName: string): string =>
  resourceName.charAt(0).toUpperCase() + resourceName.slice(1);

/**
 * Resolves the removal policy for stateful resources: RETAIN in production,
 * DESTROY for ephemeral dev stacks (secure-infrastructure rule 7).
 */
export const getRemovalPolicy = (retain: boolean): RemovalPolicy =>
  retain ? RemovalPolicy.RETAIN : RemovalPolicy.DESTROY;

/**
 * Resolves the knowledge base provider from `--context provider=s3|bedrock|quick`,
 * defaulting to `s3`. Throws fail-fast on an unrecognized value rather than
 * silently falling back.
 */
export const resolveProvider = (provider: string | undefined): Provider => {
  if (provider === undefined) {
    return Provider.S3;
  }
  const match = Object.values(Provider).find((value) => value === provider.toLowerCase());
  if (match === undefined) {
    throw new Error(
      `Invalid context value "provider=${provider}". Expected one of: ${Object.values(Provider).join(", ")}.`,
    );
  }
  return match;
};

const AWS_ACCOUNT_ID_PATTERN = /^\d{12}$/;
const isStringArray = (values: unknown): values is readonly string[] =>
  Array.isArray(values) && values.every((value) => typeof value === "string");

export interface ResolveAllowedClientAccountIdsRequest {
  readonly values: unknown;
  readonly frameworkAccount: string;
}

/** Parse, validate, and deduplicate client accounts admitted by the framework. */
export const resolveAllowedClientAccountIds = ({
  values,
  frameworkAccount,
}: ResolveAllowedClientAccountIdsRequest): readonly string[] => {
  if (values === undefined || (typeof values === "string" && values.trim().length === 0)) {
    return [];
  }
  if (typeof values !== "string" && !isStringArray(values)) {
    throw new Error(
      'Context value "allowedClientAccountIds" must be a comma-separated string of AWS account IDs.',
    );
  }
  const accountIds = typeof values === "string" ? values.split(",") : values;
  const normalizedAccountIds = accountIds.map((accountId) => accountId.trim());
  if (normalizedAccountIds.some((accountId) => !AWS_ACCOUNT_ID_PATTERN.test(accountId))) {
    throw new Error(
      'Context value "allowedClientAccountIds" must contain only comma-separated 12-digit AWS account IDs.',
    );
  }
  if (frameworkAccount.length === 0) {
    throw new Error(
      'Context value "allowedClientAccountIds" requires CDK_DEFAULT_ACCOUNT to identify the framework account.',
    );
  }
  if (normalizedAccountIds.includes(frameworkAccount)) {
    throw new Error("Allowed client accounts must not include the framework account.");
  }
  return [...new Set(normalizedAccountIds)];
};
