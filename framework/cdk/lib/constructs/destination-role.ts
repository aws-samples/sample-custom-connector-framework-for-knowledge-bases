import { Aws, RemovalPolicy, Stack, Token } from "aws-cdk-lib";
import {
  ArnPrincipal,
  CompositePrincipal,
  IPrincipal,
  ManagedPolicy,
  PolicyDocument,
  PolicyStatement,
  Role,
} from "aws-cdk-lib/aws-iam";
import { Construct } from "constructs";
import {
  ApiServiceName,
  ConnectorDeletionWorkflowName,
  DESTINATION_BUCKET_PATTERN,
  IngestionQueueName,
  RESOURCE_PREFIX,
  ROLE_PATH,
} from "../common/config";
import {
  DESTINATION_ID_TAG,
  DestinationRoleName,
  DestinationWorkflowName,
  KNOWLEDGE_BASE_METRIC_NAMESPACE,
  destinationRoleSuffix,
} from "../common/destination-names";
import {
  acknowledgeNagRules,
  DESTINATION_ROLE_WILDCARD_REASON,
  resourceFindings,
} from "../common/nag-suppressions";
import { FrameworkValues } from "./framework-values";

export interface DestinationRoleProps {
  readonly framework: FrameworkValues;
  /** The destination's name; the role is `CcfDestinationRole-<name>`, shortened with a hash past 64 characters. */
  readonly destinationName: string;
  /** For the role and both boundaries. */
  readonly removalPolicy: RemovalPolicy;
}

/**
 * One destination's service role, `CcfDestinationRole-<name>`, under `/ccf/destinations/`, with
 * two boundaries: `CcfDestinationRoleBoundary-<name>`, the role's own, which holds the same
 * statements as its inline policy, and `CcfDestinationBoundary-<name>`, which caps the knowledge
 * base role the role creates at reading `ccf-dst-*` buckets.
 *
 * The API Lambda and the ingestion consumer assume the role with the destination workflow's ARN as
 * the external ID; the destination workflow role with either destination state machine's ARN; and
 * the connector deletion workflow role with its own state machine's ARN. Every ARN uses the
 * service Region, so a stack in another Region fails synthesis.
 */
export class DestinationRole extends Construct {
  public readonly role: Role;
  public readonly roleArn: string;
  /** The knowledge base role's boundary. */
  public readonly boundary: ManagedPolicy;
  /** The destination role's own boundary. */
  public readonly roleBoundary: ManagedPolicy;

  constructor(scope: Construct, id: string, props: DestinationRoleProps) {
    super(scope, id);
    const { framework, destinationName, removalPolicy } = props;
    const stack = Stack.of(this);
    const serviceRegion = framework.frameworkApi().region;
    const regions = [stack.region, serviceRegion];
    if (!regions.some((region) => Token.isUnresolved(region)) && stack.region !== serviceRegion)
      throw new Error(
        `The destination stack is in ${stack.region}, but the framework is in ${serviceRegion}; ` +
          "deploy the destination in the framework's Region.",
      );
    const suffix = destinationRoleSuffix(destinationName);
    const boundaryName = `${DestinationRoleName.BOUNDARY_PREFIX}${suffix}`;
    const boundaryArn = `arn:${Aws.PARTITION}:iam::${stack.account}:policy${DestinationRoleName.PATH}${boundaryName}`;
    this.boundary = new ManagedPolicy(this, "Boundary", {
      managedPolicyName: boundaryName,
      path: DestinationRoleName.PATH,
      // Changing the description replaces the named policy, which fails on a deployed stack, so it stays as first written.
      description: `Caps the knowledge base role of the destination ${destinationName} at reading ccf-dst-* buckets.`,
      statements: [
        new PolicyStatement({
          sid: "DestinationBucketRead",
          actions: ["s3:GetObject*", "s3:GetBucket*", "s3:List*"],
          resources: bucketArns(),
        }),
        new PolicyStatement({
          sid: "KnowledgeBaseMetrics",
          actions: ["cloudwatch:PutMetricData"],
          resources: ["*"],
          conditions: { StringEquals: { "cloudwatch:namespace": KNOWLEDGE_BASE_METRIC_NAMESPACE } },
        }),
      ],
    });
    const statements = permissionStatements({ stack, boundaryArn });
    this.roleBoundary = new ManagedPolicy(this, "RoleBoundary", {
      managedPolicyName: `${DestinationRoleName.ROLE_BOUNDARY_PREFIX}${suffix}`,
      path: DestinationRoleName.PATH,
      description: `Caps the role of the destination ${destinationName} at its own permissions.`,
      statements,
    });
    this.role = new Role(this, "Role", {
      roleName: `${DestinationRoleName.ROLE_PREFIX}${suffix}`,
      path: DestinationRoleName.PATH,
      assumedBy: trustedPrincipals({ framework, serviceRegion }),
      permissionsBoundary: this.roleBoundary,
      description: `Lets the Custom Connector Framework create, delete, and ingest the destination ${destinationName}.`,
      inlinePolicies: {
        [DestinationRoleName.POLICY]: new PolicyDocument({ statements }),
      },
    });
    for (const resource of [this.role, this.boundary, this.roleBoundary])
      resource.applyRemovalPolicy(removalPolicy);
    this.role.node.addDependency(this.boundary);
    this.roleArn = this.role.roleArn;
    // Every wildcard here names a destination resource the workflow creates at run time; the reason lists them.
    const wildcardResources = [
      ...bucketArns(),
      ...statements.flatMap((statement) => statement.resources),
    ].filter((resource) => resource.includes("*"));
    acknowledgeNagRules({
      scopes: [this],
      acknowledgments: [
        {
          id: "AwsSolutions-IAM5",
          reason: DESTINATION_ROLE_WILDCARD_REASON,
          findings: [
            "Action::s3:GetObject*",
            "Action::s3:GetBucket*",
            "Action::s3:List*",
            ...new Set(resourceFindings({ scope: this, resources: wildcardResources })),
          ],
        },
      ],
    });
  }
}

interface FrameworkArnRequest {
  readonly framework: FrameworkValues;
  readonly serviceRegion: string;
}

interface TrustedRole {
  readonly roleName: string;
  readonly externalIds: readonly string[];
}

const bucketArns = (): string[] => [
  `arn:${Aws.PARTITION}:s3:::${DESTINATION_BUCKET_PATTERN}`,
  `arn:${Aws.PARTITION}:s3:::${DESTINATION_BUCKET_PATTERN}/*`,
];

/** Each framework role, with the external IDs it may pass. */
const trustedPrincipals = ({
  framework,
  serviceRegion,
}: FrameworkArnRequest): CompositePrincipal => {
  const { serviceAccount, destinationWorkflowArn } = framework;
  const stateMachineArn = (name: string): string =>
    `arn:${Aws.PARTITION}:states:${serviceRegion}:${serviceAccount}:stateMachine:${name}`;
  const trusted = ({ roleName, externalIds }: TrustedRole): IPrincipal =>
    new ArnPrincipal(
      `arn:${Aws.PARTITION}:iam::${serviceAccount}:role${ROLE_PATH}${roleName}`,
    ).withConditions({ StringEquals: { "sts:ExternalId": [...externalIds] } });
  return new CompositePrincipal(
    trusted({ roleName: ApiServiceName.API_ROLE, externalIds: [destinationWorkflowArn] }),
    trusted({ roleName: IngestionQueueName.CONSUMER_ROLE, externalIds: [destinationWorkflowArn] }),
    trusted({
      roleName: DestinationWorkflowName.ROLE,
      externalIds: [
        destinationWorkflowArn,
        stateMachineArn(DestinationWorkflowName.DELETION_STATE_MACHINE),
      ],
    }),
    trusted({
      roleName: ConnectorDeletionWorkflowName.ROLE,
      externalIds: [stateMachineArn(ConnectorDeletionWorkflowName.STATE_MACHINE)],
    }),
  );
};

interface PermissionsRequest {
  readonly stack: Stack;
  readonly boundaryArn: string;
}

const permissionStatements = ({ stack, boundaryArn }: PermissionsRequest): PolicyStatement[] => {
  const [buckets, objects] = bucketArns();
  const knowledgeBases = `arn:${Aws.PARTITION}:bedrock:${stack.region}:${stack.account}:knowledge-base/*`;
  // Only knowledge base roles, so the role can't change itself or another destination's role.
  const knowledgeBaseRoles = `arn:${Aws.PARTITION}:iam::${stack.account}:role${DestinationRoleName.PATH}${DestinationRoleName.KNOWLEDGE_BASE_ROLE_PREFIX}*`;
  const logs = `arn:${Aws.PARTITION}:logs:${stack.region}:${stack.account}`;
  const ingestionLogGroups = `${logs}:log-group:${DestinationWorkflowName.INGESTION_LOG_GROUP_PREFIX}${RESOURCE_PREFIX}*`;
  const taggedRequest = {
    Null: { [`aws:RequestTag/${DESTINATION_ID_TAG}`]: "false" },
    "ForAllValues:StringEquals": { "aws:TagKeys": [DESTINATION_ID_TAG] },
  };
  return [
    new PolicyStatement({
      sid: "DestinationBuckets",
      actions: [
        "s3:CreateBucket",
        "s3:DeleteBucket",
        "s3:PutBucketPolicy",
        "s3:PutEncryptionConfiguration",
        "s3:PutBucketPublicAccessBlock",
        "s3:PutBucketTagging",
        "s3:ListBucket",
      ],
      resources: [buckets],
    }),
    new PolicyStatement({
      sid: "DestinationObjects",
      actions: ["s3:DeleteObject", "s3:GetObject"],
      resources: [objects],
    }),
    new PolicyStatement({
      sid: "GlobalAccessControlList",
      actions: ["s3:PutObject"],
      resources: [
        `arn:${Aws.PARTITION}:s3:::${DESTINATION_BUCKET_PATTERN}/${DestinationWorkflowName.ACL_PREFIX}*`,
      ],
    }),
    new PolicyStatement({
      sid: "CreateTaggedKnowledgeBase",
      actions: ["bedrock:CreateKnowledgeBase"],
      resources: ["*"],
      conditions: taggedRequest,
    }),
    new PolicyStatement({
      sid: "TagTaggedKnowledgeBase",
      actions: ["bedrock:TagResource"],
      resources: [knowledgeBases],
      conditions: taggedRequest,
    }),
    new PolicyStatement({
      sid: "ManageTaggedKnowledgeBase",
      actions: [
        "bedrock:CreateDataSource",
        "bedrock:UpdateKnowledgeBase",
        "bedrock:StartIngestionJob",
        "bedrock:StopIngestionJob",
        "bedrock:AllowVendedLogDeliveryForResource",
      ],
      resources: [knowledgeBases],
      conditions: { Null: { [`aws:ResourceTag/${DESTINATION_ID_TAG}`]: "false" } },
    }),
    new PolicyStatement({
      sid: "IngestionLogGroups",
      actions: [
        "logs:CreateLogGroup",
        "logs:TagResource",
        "logs:PutRetentionPolicy",
        "logs:PutResourcePolicy",
        "logs:DeleteLogGroup",
      ],
      resources: [ingestionLogGroups, `${ingestionLogGroups}:*`],
    }),
    new PolicyStatement({
      sid: "IngestionLogDeliveries",
      actions: [
        "logs:PutDeliverySource",
        "logs:GetDeliverySource",
        "logs:DeleteDeliverySource",
        "logs:PutDeliveryDestination",
        "logs:GetDeliveryDestination",
        "logs:DeleteDeliveryDestination",
        "logs:CreateDelivery",
        "logs:GetDelivery",
        "logs:DeleteDelivery",
      ],
      resources: [
        `${logs}:delivery-source:${RESOURCE_PREFIX}*`,
        `${logs}:delivery-destination:${RESOURCE_PREFIX}*`,
        // CloudWatch Logs gives each delivery a random ID.
        `${logs}:delivery:*`,
      ],
    }),
    // These describe actions take no resource; CreateDelivery calls the last two to write the
    // log group's resource policy.
    new PolicyStatement({
      sid: "DescribeIngestionLogDelivery",
      actions: [
        "logs:DescribeDeliveries",
        "logs:DescribeResourcePolicies",
        "logs:DescribeLogGroups",
      ],
      resources: ["*"],
    }),
    // No tag condition: a knowledge base that is being deleted, or is gone, no longer resolves
    // its tags, so IAM would deny the delete path's polls and a retried delete instead of
    // Bedrock answering not found. The role reaches knowledge bases in its own account only.
    new PolicyStatement({
      sid: "ReadAndDeleteKnowledgeBases",
      actions: [
        "bedrock:GetKnowledgeBase",
        "bedrock:DeleteKnowledgeBase",
        "bedrock:GetDataSource",
        "bedrock:DeleteDataSource",
        "bedrock:GetIngestionJob",
        "bedrock:ListIngestionJobs",
      ],
      resources: [knowledgeBases],
    }),
    new PolicyStatement({
      sid: "CreateBoundedKnowledgeBaseRole",
      actions: ["iam:CreateRole"],
      resources: [knowledgeBaseRoles],
      conditions: { StringEquals: { "iam:PermissionsBoundary": boundaryArn } },
    }),
    new PolicyStatement({
      sid: "ManageKnowledgeBaseRole",
      actions: [
        "iam:TagRole",
        "iam:DeleteRole",
        "iam:PutRolePolicy",
        "iam:DeleteRolePolicy",
        "iam:GetRole",
      ],
      resources: [knowledgeBaseRoles],
    }),
    new PolicyStatement({
      sid: "PassKnowledgeBaseRole",
      actions: ["iam:PassRole"],
      resources: [knowledgeBaseRoles],
      conditions: { StringEquals: { "iam:PassedToService": "bedrock.amazonaws.com" } },
    }),
  ];
};
