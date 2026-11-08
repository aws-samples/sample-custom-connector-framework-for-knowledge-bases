import { App, Aws, RemovalPolicy, Stack } from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { DestinationRole } from "../lib/constructs/destination-role";
import {
  JsonObject,
  SERVICE_ACCOUNT,
  TENANT_ACCOUNT,
  literalFramework,
  partitionArn,
  propertiesOf,
  resourcesOf,
  statementsOf,
  tenantStack,
} from "./construct-fixtures";

interface Synthesized {
  readonly stack: Stack;
  readonly template: Template;
  readonly role: JsonObject;
  readonly boundary: JsonObject;
  readonly roleBoundary: JsonObject;
}

const managedPolicy = (template: Template, name: string): JsonObject => {
  const [policy] = resourcesOf(template, "AWS::IAM::ManagedPolicy").filter(
    (resource) => propertiesOf(resource).ManagedPolicyName === name,
  );
  return policy;
};

const synthesize = (removalPolicy = RemovalPolicy.RETAIN): Synthesized => {
  const stack = tenantStack();
  new DestinationRole(stack, "Role", {
    framework: literalFramework(),
    destinationName: "GitHub",
    removalPolicy,
  });
  const template = Template.fromStack(stack);
  const [role] = resourcesOf(template, "AWS::IAM::Role");
  return {
    stack,
    template,
    role,
    boundary: managedPolicy(template, "CcfDestinationBoundary-GitHub"),
    roleBoundary: managedPolicy(template, "CcfDestinationRoleBoundary-GitHub"),
  };
};

describe("DestinationRole", () => {
  const { stack, template, role, boundary, roleBoundary } = synthesize();
  const frameworkRole = (name: string): unknown =>
    partitionArn(stack, `iam::${SERVICE_ACCOUNT}:role/ccf/${name}`);
  const serviceStateMachine = (name: string): unknown =>
    partitionArn(stack, `states:us-east-1:${SERVICE_ACCOUNT}:stateMachine:${name}`);
  const tenantArn = (rest: string): unknown => partitionArn(stack, rest);
  const knowledgeBases = stack.resolve(
    `arn:${Aws.PARTITION}:bedrock:${Aws.REGION}:${TENANT_ACCOUNT}:knowledge-base/*`,
  );
  const knowledgeBaseRoles = tenantArn(
    `iam::${TENANT_ACCOUNT}:role/ccf/destinations/CcfKnowledgeBase-*`,
  );
  const logs = (rest: string): unknown =>
    stack.resolve(`arn:${Aws.PARTITION}:logs:${Aws.REGION}:${TENANT_ACCOUNT}:${rest}`);
  const ingestionLogGroups = "log-group:/aws/vendedlogs/bedrock/knowledge-base/ccf-*";
  const taggedRequest = {
    Null: { "aws:RequestTag/ccf:destination-id": "false" },
    "ForAllValues:StringEquals": { "aws:TagKeys": ["ccf:destination-id"] },
  };
  const inlineStatements = (): JsonObject[] => {
    const policies = propertiesOf(role).Policies as JsonObject[];
    expect(policies).toHaveLength(1);
    return statementsOf(policies[0].PolicyDocument);
  };

  it("names the role CcfDestinationRole-<name> under /ccf/destinations/", () => {
    expect(propertiesOf(role)).toMatchObject({
      RoleName: "CcfDestinationRole-GitHub",
      Path: "/ccf/destinations/",
    });
  });

  it("creates both boundaries under /ccf/destinations/", () => {
    expect(resourcesOf(template, "AWS::IAM::ManagedPolicy")).toHaveLength(2);
    expect(propertiesOf(boundary).Path).toBe("/ccf/destinations/");
    expect(propertiesOf(roleBoundary).Path).toBe("/ccf/destinations/");
  });

  it("bounds the role with CcfDestinationRoleBoundary-<name>, whose statements equal the inline policy's", () => {
    const [roleBoundaryId] = Object.keys(
      template.findResources("AWS::IAM::ManagedPolicy", {
        Properties: { ManagedPolicyName: "CcfDestinationRoleBoundary-GitHub" },
      }),
    );
    expect(propertiesOf(role).PermissionsBoundary).toEqual({ Ref: roleBoundaryId });
    expect(statementsOf(propertiesOf(roleBoundary).PolicyDocument)).toEqual(inlineStatements());
  });

  it("caps knowledge base roles at reading ccf-dst-* buckets and publishing Bedrock's knowledge base metrics", () => {
    expect(statementsOf(propertiesOf(boundary).PolicyDocument)).toEqual([
      {
        Sid: "DestinationBucketRead",
        Effect: "Allow",
        Action: ["s3:GetBucket*", "s3:GetObject*", "s3:List*"],
        Resource: [tenantArn("s3:::ccf-dst-*"), tenantArn("s3:::ccf-dst-*/*")],
      },
      {
        Sid: "KnowledgeBaseMetrics",
        Effect: "Allow",
        Action: "cloudwatch:PutMetricData",
        Resource: "*",
        Condition: { StringEquals: { "cloudwatch:namespace": "AWS/Bedrock/KnowledgeBases" } },
      },
    ]);
  });

  it("gives each service role exactly its external IDs", () => {
    const trust = propertiesOf(role).AssumeRolePolicyDocument as { Statement: JsonObject[] };
    const asArray = (value: unknown): unknown[] => (Array.isArray(value) ? value : [value]);
    const externalIds = Object.fromEntries(
      trust.Statement.flatMap((statement) => {
        expect(statement).toMatchObject({ Effect: "Allow", Action: "sts:AssumeRole" });
        const principals = asArray((statement.Principal as { AWS: unknown }).AWS);
        const condition = statement.Condition as { StringEquals: { "sts:ExternalId": unknown } };
        const ids = asArray(condition.StringEquals["sts:ExternalId"]);
        return principals.map((principal) => [JSON.stringify(principal), ids]);
      }),
    );
    const destinationWorkflow = `arn:aws:states:us-east-1:${SERVICE_ACCOUNT}:stateMachine:CcfDestinationWorkflow`;
    expect(externalIds).toEqual({
      [JSON.stringify(frameworkRole("CcfApiRole"))]: [destinationWorkflow],
      [JSON.stringify(frameworkRole("CcfIngestionConsumerRole"))]: [destinationWorkflow],
      [JSON.stringify(frameworkRole("CcfDestinationWorkflowRole"))]: [
        destinationWorkflow,
        serviceStateMachine("CcfDestinationDeletionWorkflow"),
      ],
      [JSON.stringify(frameworkRole("CcfConnectorDeletionWorkflowRole"))]: [
        serviceStateMachine("CcfConnectorDeletionWorkflow"),
      ],
    });
  });

  it("fails synthesis in another Region than the framework's, naming both", () => {
    const elsewhere = new Stack(new App(), "Elsewhere", {
      env: { account: TENANT_ACCOUNT, region: "eu-west-1" },
    });
    expect(
      () =>
        new DestinationRole(elsewhere, "Role", {
          framework: literalFramework(),
          destinationName: "GitHub",
          removalPolicy: RemovalPolicy.DESTROY,
        }),
    ).toThrow(/eu-west-1.*us-east-1/);
  });

  it("creates knowledge base roles only with its own boundary, and manages only knowledge base roles", () => {
    expect(inlineStatements()).toEqual([
      {
        Sid: "DestinationBuckets",
        Effect: "Allow",
        Action: [
          "s3:CreateBucket",
          "s3:DeleteBucket",
          "s3:ListBucket",
          "s3:PutBucketPolicy",
          "s3:PutBucketPublicAccessBlock",
          "s3:PutBucketTagging",
          "s3:PutEncryptionConfiguration",
        ],
        Resource: tenantArn("s3:::ccf-dst-*"),
      },
      {
        Sid: "DestinationObjects",
        Effect: "Allow",
        Action: ["s3:DeleteObject", "s3:GetObject"],
        Resource: tenantArn("s3:::ccf-dst-*/*"),
      },
      {
        Sid: "GlobalAccessControlList",
        Effect: "Allow",
        Action: "s3:PutObject",
        Resource: tenantArn("s3:::ccf-dst-*/ccf-acl/*"),
      },
      {
        Sid: "CreateTaggedKnowledgeBase",
        Effect: "Allow",
        Action: "bedrock:CreateKnowledgeBase",
        Resource: "*",
        Condition: taggedRequest,
      },
      {
        Sid: "TagTaggedKnowledgeBase",
        Effect: "Allow",
        Action: "bedrock:TagResource",
        Resource: knowledgeBases,
        Condition: taggedRequest,
      },
      {
        Sid: "ManageTaggedKnowledgeBase",
        Effect: "Allow",
        Action: [
          "bedrock:AllowVendedLogDeliveryForResource",
          "bedrock:CreateDataSource",
          "bedrock:StartIngestionJob",
          "bedrock:StopIngestionJob",
          "bedrock:UpdateKnowledgeBase",
        ],
        Resource: knowledgeBases,
        Condition: { Null: { "aws:ResourceTag/ccf:destination-id": "false" } },
      },
      {
        Sid: "IngestionLogGroups",
        Effect: "Allow",
        Action: [
          "logs:CreateLogGroup",
          "logs:DeleteLogGroup",
          "logs:PutResourcePolicy",
          "logs:PutRetentionPolicy",
          "logs:TagResource",
        ],
        Resource: [logs(ingestionLogGroups), logs(`${ingestionLogGroups}:*`)],
      },
      {
        Sid: "IngestionLogDeliveries",
        Effect: "Allow",
        Action: [
          "logs:CreateDelivery",
          "logs:DeleteDelivery",
          "logs:DeleteDeliveryDestination",
          "logs:DeleteDeliverySource",
          "logs:GetDelivery",
          "logs:GetDeliveryDestination",
          "logs:GetDeliverySource",
          "logs:PutDeliveryDestination",
          "logs:PutDeliverySource",
        ],
        Resource: [
          logs("delivery-destination:ccf-*"),
          logs("delivery-source:ccf-*"),
          logs("delivery:*"),
        ],
      },
      {
        Sid: "DescribeIngestionLogDelivery",
        Effect: "Allow",
        Action: [
          "logs:DescribeDeliveries",
          "logs:DescribeLogGroups",
          "logs:DescribeResourcePolicies",
        ],
        Resource: "*",
      },
      {
        Sid: "ReadAndDeleteKnowledgeBases",
        Effect: "Allow",
        Action: [
          "bedrock:DeleteDataSource",
          "bedrock:DeleteKnowledgeBase",
          "bedrock:GetDataSource",
          "bedrock:GetIngestionJob",
          "bedrock:GetKnowledgeBase",
          "bedrock:ListIngestionJobs",
        ],
        Resource: knowledgeBases,
      },
      {
        Sid: "CreateBoundedKnowledgeBaseRole",
        Effect: "Allow",
        Action: "iam:CreateRole",
        Resource: knowledgeBaseRoles,
        Condition: {
          StringEquals: {
            "iam:PermissionsBoundary": tenantArn(
              `iam::${TENANT_ACCOUNT}:policy/ccf/destinations/CcfDestinationBoundary-GitHub`,
            ),
          },
        },
      },
      {
        Sid: "ManageKnowledgeBaseRole",
        Effect: "Allow",
        Action: [
          "iam:DeleteRole",
          "iam:DeleteRolePolicy",
          "iam:GetRole",
          "iam:PutRolePolicy",
          "iam:TagRole",
        ],
        Resource: knowledgeBaseRoles,
      },
      {
        Sid: "PassKnowledgeBaseRole",
        Effect: "Allow",
        Action: "iam:PassRole",
        Resource: knowledgeBaseRoles,
        Condition: { StringEquals: { "iam:PassedToService": "bedrock.amazonaws.com" } },
      },
    ]);
  });

  it("is created after the knowledge base role's boundary", () => {
    const boundaryId = Object.keys(
      template.findResources("AWS::IAM::ManagedPolicy", {
        Properties: { ManagedPolicyName: "CcfDestinationBoundary-GitHub" },
      }),
    )[0];

    expect(role.DependsOn).toEqual(expect.arrayContaining([boundaryId]));
  });

  it.each([
    [RemovalPolicy.RETAIN, "Retain"],
    [RemovalPolicy.DESTROY, "Delete"],
  ])("applies %s to the role and both boundaries", (removalPolicy, deletionPolicy) => {
    const synthesized = synthesize(removalPolicy);

    expect([
      synthesized.role.DeletionPolicy,
      synthesized.boundary.DeletionPolicy,
      synthesized.roleBoundary.DeletionPolicy,
    ]).toEqual([deletionPolicy, deletionPolicy, deletionPolicy]);
  });
});
