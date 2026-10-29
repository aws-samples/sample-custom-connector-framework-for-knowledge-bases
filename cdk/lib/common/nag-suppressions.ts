import { NagPackSuppression } from "cdk-nag";

/**
 * Stack-level cdk-nag suppressions, each narrowly scoped and justified. Prefer
 * per-resource suppressions inside the owning construct group (as the
 * KnowledgeBase does for its KB-role read grant) so this list stays small and
 * every suppression is auditable. Populated from the findings a real
 * `cdk synth` surfaces, not guessed.
 */
export const NAG_SUPPRESSIONS: NagPackSuppression[] = [
  {
    id: "AwsSolutions-IAM4",
    reason:
      "CDK-managed roles use AWS-managed policies the project neither authors nor " +
      "controls: the backend Lambda execution roles use AWSLambdaBasicExecutionRole, " +
      "and API Gateway's account CloudWatch role uses AmazonAPIGatewayPushToCloudWatchLogs.",
    appliesTo: [
      "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
      "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AmazonAPIGatewayPushToCloudWatchLogs",
    ],
  },
  {
    id: "AwsSolutions-IAM5",
    reason:
      "Expansions of CDK grant methods and narrowly bounded dynamic namespaces: " +
      "the ECR authorization-token and Batch DescribeJobDefinitions actions require Resource:* " +
      "because neither supports resource-level permissions; " +
      "the S3 write grant expands action and object-key wildcards for the single ingestion bucket; " +
      "the framework execution role pulls only the configured client bootstrap asset " +
      "repositories in the active Region; its log delivery is limited to streams under the AWS " +
      "Batch standard /aws/batch/job log group; Scheduler " +
      "lifecycle is limited to one deterministic group; Batch job-definition names and job ids " +
      "are service-generated, so job access uses job/* constrained by an exact CCF ownership tag. " +
      "Two further wildcards exist because the resources are created after synthesis. " +
      "Connector runtime roles are minted per connector at registration time, so the control " +
      "plane's iam:CreateRole/PutRolePolicy/DeleteRolePolicy/DeleteRole and the orchestrator's " +
      "iam:PassRole name the service-owned role/ccf-runtime/CcfConnectorRuntime-* path prefix " +
      "rather than one unknowable role name; CreateRole additionally requires the " +
      "CcfConnectorRuntimeBoundary permissions boundary through an IAM condition, and no role " +
      "may attach managed policies or alter a boundary. The boundary itself must bound every " +
      "connector at once, so it caps the documents/ prefix tree on the one ingestion bucket and " +
      "the control-plane path on this deployment's own API; each per-connector inline policy, " +
      "written by the control plane, narrows that to exactly one connector id, REST API id, " +
      "route set, destination prefix, and registered source role.",
    appliesTo: [
      "Action::s3:DeleteObject*",
      "Action::s3:Abort*",
      "Action::dynamodb:BatchGet*",
      "Action::dynamodb:BatchWrite*",
      "Action::kms:GenerateDataKey*",
      "Action::kms:ReEncrypt*",
      { regex: "/^Resource::\\*$/" },
      { regex: "/^Resource::<.*(Table|CustomConnector).*\\.Arn>/index/\\*$/" },
      { regex: "/^Resource::<.*IngestionBucket.*\\.Arn>/\\*$/" },
      { regex: "/^Resource::<.*IngestionBucket.*\\.Arn>/documents/\\*$/" },
      { regex: "/^Resource::arn:(<AWS::Partition>|aws[a-z-]*):batch:.*:job/\\*$/" },
      {
        regex: "/^Resource::arn:(<AWS::Partition>|aws[a-z-]*):batch:.*:job-definition/ccj-\\*.*$/",
      },
      {
        regex:
          "/^Resource::arn:(<AWS::Partition>|aws[a-z-]*):iam::[0-9]{12}:role/CcfSourceRole-\\*$/",
      },
      {
        regex:
          "/^Resource::arn:(<AWS::Partition>|aws[a-z-]*):iam::[0-9]{12}:role/ccf-runtime/CcfConnectorRuntime-\\*$/",
      },
      {
        regex:
          "/^Resource::arn:(<AWS::Partition>|aws[a-z-]*):logs:[^:]+:[0-9]{12}:log-group:/aws/batch/job:log-stream:\\*$/",
      },
      {
        regex:
          "/^Resource::arn:(<AWS::Partition>|aws[a-z-]*):execute-api:[^:]+:[0-9]{12}:\\*/\\*/\\*/api/v1/custom-connectors/\\*$/",
      },
      {
        regex:
          "/^Resource::arn:(<AWS::Partition>|aws[a-z-]*):scheduler:.*:schedule/CcfSchedulerGroup/\\*$/",
      },
    ],
  },
  {
    id: "AwsSolutions-SQS3",
    reason:
      "These three encrypted queues are terminal DLQs for Scheduler, DynamoDB Streams, and " +
      "EventBridge delivery. A DLQ for a terminal DLQ would only move the same failed event " +
      "again and would not add a recovery path.",
  },
  {
    id: "AwsSolutions-L1",
    reason:
      "The backend Lambdas are pinned to the Python 3.14 runtime to match the backend " +
      "package's own runtime and its pinned requirements.txt; the runtime is upgraded " +
      "deliberately alongside the backend, not implicitly to the newest available.",
  },
  {
    id: "AwsSolutions-APIG2",
    reason:
      "Request validation is performed in the backend by the AWS Lambda Powertools " +
      "router behind a single proxy integration; gateway-level request-body models are " +
      "intentionally not duplicated.",
  },
  {
    id: "AwsSolutions-APIG3",
    reason:
      "Every API method requires IAM (SigV4) authorization, and the resource policy admits only " +
      "the framework account plus explicitly configured client account principals. A WAF web ACL " +
      "is recommended defense-in-depth for a production, internet-exposed deployment and can be " +
      "without code changes.",
  },
  {
    id: "AwsSolutions-COG4",
    reason:
      "The API deliberately uses IAM (SigV4) authorization, not a Cognito user pool " +
      "authorizer, so callers authenticate with AWS credentials scoped by the account " +
      "resource policy.",
  },
];
