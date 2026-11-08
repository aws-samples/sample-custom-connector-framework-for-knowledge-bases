import { Arn, ArnFormat, Stack, Validations } from "aws-cdk-lib";
import { IStateMachine } from "aws-cdk-lib/aws-stepfunctions";
// cdk-nag's own flattening, so a finding computed here is the string cdk-nag reports; cdk-nag is pinned exactly.
import { flattenCfnReference } from "cdk-nag/lib/utils/flatten-cfn-reference";
import { IConstruct } from "constructs";
import { DESTINATION_BUCKET_PATTERN } from "./config";
import { DESTINATION_ID_TAG, DestinationRoleName } from "./destination-names";

/**
 * A cdk-nag rule a construct breaks on purpose, and why. `findings` names each finding the
 * acknowledgment covers, such as `Resource::*`; cdk-nag 3 matches each one exactly. Without
 * `findings`, it covers a rule that reports no findings.
 */
export interface NagAcknowledgment {
  readonly id: string;
  readonly reason: string;
  readonly findings?: readonly string[];
}

export interface AcknowledgeNagRulesRequest {
  /** The constructs to acknowledge on; each covers its children. */
  readonly scopes: readonly IConstruct[];
  readonly acknowledgments: readonly NagAcknowledgment[];
}

/** Records each acknowledgment with CDK's `Validations`, which cdk-nag reads on the construct and its ancestors. */
export const acknowledgeNagRules = ({
  scopes,
  acknowledgments,
}: AcknowledgeNagRulesRequest): void => {
  const rules = acknowledgments.flatMap(({ id, reason, findings }) =>
    findings === undefined
      ? [{ id, reason }]
      : findings.map((finding) => ({ id: `${id}[${finding}]`, reason })),
  );
  for (const scope of scopes) Validations.of(scope).acknowledge(...rules);
};

export interface ResourceFindingsRequest {
  readonly scope: IConstruct;
  /** The resource ARNs as the policy statement names them, tokens included. */
  readonly resources: readonly string[];
}

/**
 * The `Resource::` finding cdk-nag reports for each ARN. cdk-nag 3 matches findings exactly, so
 * each is flattened from the same value the statement uses, with cdk-nag's own flattening.
 */
export const resourceFindings = ({ scope, resources }: ResourceFindingsRequest): string[] =>
  resources.map(
    (resource) => `Resource::${flattenCfnReference(Stack.of(scope).resolve(resource))}`,
  );
const DESTINATION_ROLES = `role${DestinationRoleName.PATH}${DestinationRoleName.ROLE_PREFIX}*`;
const KNOWLEDGE_BASE_ROLES = `role${DestinationRoleName.PATH}${DestinationRoleName.KNOWLEDGE_BASE_ROLE_PREFIX}*`;

/** The acknowledgment for a role whose only `Resource: "*"` statements are actions that take no resource. */
export const actionsWithoutResourceAcknowledgment = (
  actions: readonly string[],
): NagAcknowledgment => ({
  id: "AwsSolutions-IAM5",
  reason: `${actions.join(", ")} take no resource, so IAM requires "*" for them.`,
  findings: ["Resource::*"],
});

/** X-Ray's trace writes, which Lambda's active tracing adds to every function role. */
export const XRAY_TRACE_ACTIONS = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"] as const;

/**
 * `Resource: "*"` on actions that take no resource, on a workflow role: `cloudwatch:PutMetricData`
 * (allowed only in the framework's namespace), and the X-Ray and log delivery actions CDK adds for
 * tracing and execution logging.
 */
export const WORKFLOW_ROLE_STAR_ACKNOWLEDGMENT: NagAcknowledgment = {
  id: "AwsSolutions-IAM5",
  reason:
    "cloudwatch:PutMetricData, the X-Ray trace actions, and the CloudWatch Logs delivery actions take no " +
    "resource; PutMetricData is allowed only in the framework's metric namespace.",
  findings: ["Resource::*"],
};

/**
 * The finding for a `grantExecution` on a state machine: CDK names the machine's executions as
 * `execution:<name>:*`, with the name split from the machine's ARN.
 */
export const executionsFinding = (stateMachine: IStateMachine): string => {
  const stack = Stack.of(stateMachine);
  const name = Arn.split(stateMachine.stateMachineArn, ArnFormat.COLON_RESOURCE_NAME).resourceName;
  const executions = stack.formatArn({
    service: "states",
    resource: "execution",
    resourceName: `${name}:*`,
    arnFormat: ArnFormat.COLON_RESOURCE_NAME,
  });
  return `Resource::${flattenCfnReference(stack.resolve(executions))}`;
};

/** The reason for each of the four roles that assume destination roles: the API, both workflows, and the ingestion consumer. */
export const ASSUME_DESTINATION_ROLES_REASON =
  "Destination roles are created per destination, so the roles that assume them name " +
  `${DESTINATION_ROLES}, and each destination role's trust policy names them ` +
  "back with the destination workflow's ARN as the external ID.";

/** The acknowledgment for a role that assumes `destinationRoleArns(...)`. */
export const assumeDestinationRolesAcknowledgment = (
  request: ResourceFindingsRequest,
): NagAcknowledgment => ({
  id: "AwsSolutions-IAM5",
  reason: ASSUME_DESTINATION_ROLES_REASON,
  findings: resourceFindings(request),
});

/**
 * Stack-level acknowledgments for `CustomConnectorFramework`: the expansions CDK's grant methods
 * produce, and the findings that hold for the whole stack. A wildcard one construct owns is
 * acknowledged on that construct's role, beside the statement.
 */
export const FRAMEWORK_ACKNOWLEDGMENTS: readonly NagAcknowledgment[] = [
  {
    id: "AwsSolutions-IAM4",
    reason:
      "The framework's Lambda roles use AWSLambdaBasicExecutionRole for their own log groups, and API " +
      "Gateway's account role uses AmazonAPIGatewayPushToCloudWatchLogs; both are AWS managed policies.",
    findings: [
      "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
      "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AmazonAPIGatewayPushToCloudWatchLogs",
    ],
  },
  {
    id: "AwsSolutions-IAM5",
    reason:
      "Expansions of CDK grant methods: a table grant covers DynamoDB's batch actions, and a key grant " +
      "covers KMS's data key and re-encrypt actions.",
    findings: [
      "Action::dynamodb:BatchGet*",
      "Action::dynamodb:BatchWrite*",
      "Action::kms:GenerateDataKey*",
      "Action::kms:ReEncrypt*",
    ],
  },
  {
    id: "AwsSolutions-SF1",
    reason:
      "The two workflows log at ERROR with execution data: every failure's log event " +
      "carries its input's correlation ID, and logging every state transition would store each input again.",
  },
  {
    id: "AwsSolutions-SQS3",
    reason:
      "The scheduler and ingestion dead-letter queues are terminal. A dead-letter queue for a dead-letter " +
      "queue would move the same failed message again without a new way to recover it.",
  },
  {
    id: "AwsSolutions-L1",
    reason:
      "The backend Lambdas use the Python 3.14 runtime the backend package and its uv.lock target; the " +
      "runtime is upgraded with the backend, not implicitly to the newest one.",
  },
  {
    id: "AwsSolutions-APIG2",
    reason:
      "API Gateway validates every request against the OpenAPI body's request validator (`all`), which cdk-nag " +
      "doesn't detect on a SpecRestApi. The backend's parse into the generated models is the parse its " +
      "handlers need to read the request, not a second validation layer.",
  },
  {
    id: "AwsSolutions-APIG3",
    reason:
      "Every method requires IAM (SigV4) authorization, the stage is throttled, and the resource policy admits " +
      "only the service account and the configured tenant accounts. The framework uses no AWS WAF (a recorded deviation).",
  },
  {
    id: "AwsSolutions-COG4",
    reason:
      "The API uses IAM (SigV4) authorization, not a Cognito user pool authorizer: callers authenticate with " +
      "AWS credentials, scoped by the resource policy.",
  },
];

/**
 * Acknowledgments for the client constructs' custom resource providers (`Destination`'s and
 * `CustomConnector`'s), on each provider construct.
 */
export const CONNECTOR_OPS_ACKNOWLEDGMENTS: readonly NagAcknowledgment[] = [
  {
    id: "AwsSolutions-IAM4",
    reason:
      "The provider's Lambda roles use AWSLambdaBasicExecutionRole, an AWS managed policy, for their own log groups.",
    findings: [
      "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
    ],
  },
  {
    id: "AwsSolutions-L1",
    reason:
      "The handlers use the Python 3.14 runtime that framework/cdk's uv.lock targets; the CDK Provider framework " +
      "picks its own Node.js runtime.",
  },
  {
    id: "AwsSolutions-SF1",
    reason:
      "The CDK Provider framework's waiter state machine logs errors, which is all a retry loop records.",
  },
  {
    id: "AwsSolutions-SF2",
    reason:
      "The CDK Provider framework's waiter state machine only polls isComplete; X-Ray adds nothing to it.",
  },
];

/** The reason for a provider's wildcard grants; each provider names its own findings. */
export const CONNECTOR_OPS_WILDCARD_REASON =
  "Each handler may call only the framework API routes the API Reference's caller table lists for it: the * segments are " +
  "the stage and the destination and custom connector IDs, which the framework assigns at run time. The CDK " +
  "Provider framework invokes only versions of its own handler functions, and its waiter state machine " +
  "writes only to its log group through log delivery actions that take no resource.";

/** The destination role's acknowledgments; the role adds the findings that name its own resources. */
export const DESTINATION_ROLE_WILDCARD_REASON =
  "The destination workflow creates the destination's bucket, knowledge base, knowledge base role, and " +
  `ingestion log delivery at run time, so the destination role names them by pattern: buckets ${DESTINATION_BUCKET_PATTERN}, roles ` +
  `${KNOWLEDGE_BASE_ROLES} (CreateRole requires this destination's own boundary), ` +
  `knowledge bases tagged ${DESTINATION_ID_TAG}, and log groups, delivery sources, and delivery destinations named ` +
  "for the destination; CloudWatch Logs names each delivery with a random ID. bedrock:CreateKnowledgeBase is on * " +
  "because the Service Authorization Reference lists no resource type for it; its aws:RequestTag and aws:TagKeys " +
  "conditions allow only tagged creates. The describe actions CreateDelivery calls take no resource. The " +
  "knowledge base role's boundary allows the s3:GetObject*, s3:GetBucket*, and s3:List* read verbs CDK's grantRead " +
  `produces on ${DESTINATION_BUCKET_PATTERN} buckets only, and PutMetricData in Bedrock's knowledge base namespace only.`;

/** Acknowledgments for a `ConnectorImage`'s build project and its role; the provider has `CONNECTOR_OPS_ACKNOWLEDGMENTS`. */
export const CONNECTOR_IMAGE_ACKNOWLEDGMENTS: readonly NagAcknowledgment[] = [
  {
    id: "AwsSolutions-CB3",
    reason: "docker build needs the Docker daemon, which CodeBuild runs only in privileged mode.",
  },
  {
    id: "AwsSolutions-CB4",
    reason:
      "The build produces no CodeBuild artifact: its only output is the image it pushes to ECR, which " +
      "encrypts it at rest.",
  },
];

/** The reason for the wildcards of a `ConnectorImage`'s build role; the image names its findings. */
export const CONNECTOR_IMAGE_WILDCARD_REASON =
  "ecr:GetAuthorizationToken takes no resource, and the build writes only to its own log group's streams.";

/** The reason for a `CustomConnector`'s source-access and log-delivery role wildcards; the connector names its findings. */
export const CONNECTOR_ROLE_WILDCARD_REASON =
  "The source-access role reaches only objects under its own custom connector's connector_id=<ID>/ prefix of " +
  "the destination bucket, whose keys the job creates. The log-delivery role writes only to its own log " +
  "group's streams, which the log router names per job.";
