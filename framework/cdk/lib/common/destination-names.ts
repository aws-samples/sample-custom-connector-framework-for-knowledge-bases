import { createHash } from "node:crypto";
import { Aws } from "aws-cdk-lib";
import { RESOURCE_PREFIX } from "./config";

/** The destination workflows' fixed names, which other accounts' policies name. */
export enum DestinationWorkflowName {
  STATE_MACHINE = "CcfDestinationWorkflow",
  DELETION_STATE_MACHINE = "CcfDestinationDeletionWorkflow",
  ROLE = "CcfDestinationWorkflowRole",
  KNOWLEDGE_BASE_ROLE_POLICY = "CcfKnowledgeBaseBucketRead",
  GLOBAL_ACL_KEY = "ccf-acl/global-acl.json",
  ACL_PREFIX = "ccf-acl/",
  /** Under `/aws/vendedlogs/`, so the delivery's log group resource policy stays one wildcard statement. */
  INGESTION_LOG_GROUP_PREFIX = "/aws/vendedlogs/bedrock/knowledge-base/",
}

/** A destination's role, boundaries, and knowledge base role: the names the API's `role_arn` check expects. */
export enum DestinationRoleName {
  ROLE_PREFIX = "CcfDestinationRole-",
  BOUNDARY_PREFIX = "CcfDestinationBoundary-",
  ROLE_BOUNDARY_PREFIX = "CcfDestinationRoleBoundary-",
  PATH = "/ccf/destinations/",
  POLICY = "CcfDestinationPermissions",
  KNOWLEDGE_BASE_ROLE_PREFIX = "CcfKnowledgeBase-",
}

/** The workflow input's `action`; the backend's `DestinationWorkflowAction` holds the same two values. */
export enum DestinationWorkflowAction {
  CREATE = "CREATE",
  DELETE = "DELETE",
}

/** The tag on every resource the workflow creates in the tenant account. */
export const DESTINATION_ID_TAG = "ccf:destination-id";

/** The namespace Bedrock publishes a managed knowledge base's metrics in, as its service role. */
export const KNOWLEDGE_BASE_METRIC_NAMESPACE = "AWS/Bedrock/KnowledgeBases";
/** The log type of a managed knowledge base's ingestion logs. */
export const KNOWLEDGE_BASE_LOG_TYPE = "APPLICATION_LOGS";

/** The JSONata expression of a destination's ingestion log group name, given its destination ID expression. */
export const ingestionLogGroupNameExpression = (destinationId: string): string =>
  `'${DestinationWorkflowName.INGESTION_LOG_GROUP_PREFIX}${RESOURCE_PREFIX}' & ${destinationId}`;

const MAX_ROLE_NAME_LENGTH = 64;
const NAME_HASH_LENGTH = 8;
/** The API's destination name rule. */
const DESTINATION_NAME_PATTERN = /^[A-Za-z0-9._-]{1,64}$/;

export interface DestinationAccountsRequest {
  readonly serviceAccountId: string;
  readonly allowedTenantAccountIds: readonly string[];
}

/** Every destination role in each account the framework serves: `role/ccf/destinations/CcfDestinationRole-*`. */
export const destinationRoleArns = (request: DestinationAccountsRequest): string[] =>
  [...new Set([request.serviceAccountId, ...request.allowedTenantAccountIds])].map(
    (account) =>
      `arn:${Aws.PARTITION}:iam::${account}:role${DestinationRoleName.PATH}${DestinationRoleName.ROLE_PREFIX}*`,
  );

/** The part after the prefixes: the name, or as much as fits with a hyphen and 8 hex of its SHA-256. */
export const destinationRoleSuffix = (destinationName: string): string => {
  if (!DESTINATION_NAME_PATTERN.test(destinationName))
    throw new Error(
      `destination name "${destinationName}" must be 1 to 64 letters, digits, '.', '_', or '-'.`,
    );
  const room = MAX_ROLE_NAME_LENGTH - DestinationRoleName.ROLE_PREFIX.length;
  if (destinationName.length <= room) return destinationName;
  const hash = createHash("sha256")
    .update(destinationName)
    .digest("hex")
    .slice(0, NAME_HASH_LENGTH);
  return `${destinationName.slice(0, room - NAME_HASH_LENGTH - 1)}-${hash}`;
};

/** A destination's bucket: in `destinationBucketName` literal values, in its JSONata form JSONata expressions. */
export interface DestinationBucketNameRequest {
  readonly destinationId: string;
  readonly account: string;
  readonly region: string;
}

/** `ccf-<destination ID>-<account>-<region>`, at synthesis. */
export const destinationBucketName = (request: DestinationBucketNameRequest): string =>
  `${RESOURCE_PREFIX}${request.destinationId}-${request.account}-${request.region}`;

/** The same name, built in Step Functions from three JSONata expressions. */
export const destinationBucketNameJsonata = (request: DestinationBucketNameRequest): string =>
  `{% '${RESOURCE_PREFIX}' & ${request.destinationId} & '-' & ${request.account} & '-' & ${request.region} %}`;
