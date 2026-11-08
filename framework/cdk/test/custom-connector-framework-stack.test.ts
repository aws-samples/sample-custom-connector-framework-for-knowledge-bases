import { resolve } from "node:path";
import { Arn, ArnFormat, Aws, RemovalPolicy } from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import { RetentionDays } from "aws-cdk-lib/aws-logs";
import { loadFrameworkAppConfig } from "../lib/common/app-config";
import { ProjectName, SERVICE_SETTINGS } from "../lib/common/config";
import { FrameworkExport } from "../lib/common/framework-exports";
import { SERVICE_ROLE_SERVICES } from "../lib/constructs/service-role-boundary";
import {
  CustomConnectorFrameworkStack,
  CustomConnectorFrameworkStackProps,
} from "../lib/stacks/custom-connector-framework-stack";
import { createApp, settingsOf } from "./construct-fixtures";

type JsonObject = Record<string, unknown>;

const SERVICE_ACCOUNT = "444455556666";
const TENANT_ACCOUNT = "111122223333";
const config = loadFrameworkAppConfig({
  path: resolve(__dirname, "..", "config", "example-disposable.json"),
  serviceAccount: SERVICE_ACCOUNT,
});

// The app-level settings (cost center, alarm thresholds) ride along unread: the stack's props omit them.
const props: CustomConnectorFrameworkStackProps = {
  ...config,
  projectName: ProjectName.CCF,
  removalPolicy: RemovalPolicy.DESTROY,
  logRetention: RetentionDays.ONE_WEEK,
  region: "us-east-1",
  deletionProtectionThreshold: 15,
  // No Region: every ARN must use the partition token, never a literal `arn:aws:`.
  env: { account: SERVICE_ACCOUNT },
};
const stack = new CustomConnectorFrameworkStack(createApp(), "CustomConnectorFramework", props);
const template = Template.fromStack(stack);
const json = template.toJSON() as {
  Resources: Record<string, { Type: string; Properties: JsonObject }>;
  Outputs: Record<string, JsonObject>;
};
const resolved = (value: string): unknown => stack.resolve(value);
const arn = (rest: string): unknown => resolved(`arn:${Aws.PARTITION}:${rest}`);

const logicalId = (type: string, properties: JsonObject): string => {
  const ids = Object.keys(template.findResources(type, { Properties: properties }));
  expect(ids).toHaveLength(1);
  return ids[0];
};
const tableArn = (name: string): unknown => ({
  "Fn::GetAtt": [logicalId("AWS::DynamoDB::Table", { TableName: name }), "Arn"],
});
const tableArnId = (name: string): string => logicalId("AWS::DynamoDB::Table", { TableName: name });
const keyArn = (): unknown => ({
  "Fn::GetAtt": [Object.keys(template.findResources("AWS::KMS::Key"))[0], "Arn"],
});
const queueArn = (name: string): unknown => ({
  "Fn::GetAtt": [logicalId("AWS::SQS::Queue", { QueueName: name }), "Arn"],
});
const stateMachineRef = (name: string): unknown => ({
  Ref: logicalId("AWS::StepFunctions::StateMachine", { StateMachineName: name }),
});
const frameworkDestinationRoles = [
  resolved(
    `arn:${Aws.PARTITION}:iam::${Aws.ACCOUNT_ID}:role/ccf/destinations/CcfDestinationRole-*`,
  ),
  arn(`iam::${TENANT_ACCOUNT}:role/ccf/destinations/CcfDestinationRole-*`),
];

interface Statement {
  readonly Action: string[];
  readonly Resource: unknown[];
  readonly Condition?: unknown;
}
const asArray = (value: unknown): unknown[] => (Array.isArray(value) ? value : [value]);

/**
 * Every identity statement of the role with the given name: its inline policies and attached
 * `AWS::IAM::Policy`s. Each must be an Allow, so the comparisons below can leave the effect out.
 */
const roleStatements = (roleName: string): Statement[] => {
  const roleId = logicalId("AWS::IAM::Role", { RoleName: roleName });
  const inline = (
    (json.Resources[roleId].Properties.Policies as
      { PolicyDocument: { Statement: JsonObject[] } }[] | undefined) ?? []
  ).flatMap((policy) => policy.PolicyDocument.Statement);
  const attached = Object.values(template.findResources("AWS::IAM::Policy"))
    .filter((policy) =>
      (policy.Properties.Roles as { Ref: string }[]).some((role) => role.Ref === roleId),
    )
    .flatMap((policy) => policy.Properties.PolicyDocument.Statement as JsonObject[]);
  const statements = [...inline, ...attached];
  expect(statements.map((statement) => statement.Effect)).toEqual(statements.map(() => "Allow"));
  return statements.map((statement) => ({
    Action: asArray(statement.Action) as string[],
    Resource: asArray(statement.Resource),
    ...(statement.Condition === undefined ? {} : { Condition: statement.Condition }),
  }));
};

const same = (first: unknown, second: unknown): boolean =>
  JSON.stringify(first) === JSON.stringify(second);

/** A policy document with each statement's actions and resources in a fixed order, as `minimizePolicies` may reorder them. */
const normalized = (document: {
  readonly Version?: string;
  readonly Statement: JsonObject[];
}): unknown => ({
  ...document,
  Statement: document.Statement.map((statement) => ({
    ...statement,
    Action: asArray(statement.Action).map(String).sort(),
    Resource: asArray(statement.Resource)
      .map((resource) => JSON.stringify(resource))
      .sort(),
  })),
});

/**
 * The role holds each expected statement (all its actions on exactly its resources, with its
 * condition), and no statement grants one of those actions on any other resource.
 */
const expectExactly = (roleName: string, expected: Statement[]): void => {
  const statements = roleStatements(roleName);
  for (const wanted of expected) {
    const holder = statements.find(
      (statement) =>
        wanted.Action.every((action) => statement.Action.includes(action)) &&
        same([...statement.Resource].map(String).sort(), [...wanted.Resource].map(String).sort()) &&
        same(statement.Condition, wanted.Condition),
    );
    expect({ roleName, wanted, held: holder !== undefined }).toEqual({
      roleName,
      wanted,
      held: true,
    });
  }
  for (const statement of statements)
    for (const action of statement.Action) {
      const allowed = expected
        .filter((wanted) => wanted.Action.includes(action))
        .flatMap((wanted) => wanted.Resource);
      if (allowed.length === 0) continue;
      for (const resource of statement.Resource)
        expect({
          roleName,
          action,
          resource,
          allowed: allowed.some((candidate) => same(candidate, resource)),
        }).toEqual({
          roleName,
          action,
          resource,
          allowed: true,
        });
    }
};

const functions = Object.values(template.findResources("AWS::Lambda::Function")).map(
  (resource) =>
    resource.Properties as {
      FunctionName?: string;
      Architectures: string[];
      Environment?: { Variables: Record<string, unknown> };
    },
);
const environment = (functionName: string): Record<string, unknown> => {
  const found = functions.find((properties) => properties.FunctionName === functionName);
  if (found === undefined) throw new Error(`No function ${functionName}`);
  return found.Environment?.Variables ?? {};
};

describe("CustomConnectorFramework", () => {
  it("throttles the API stage at the configured rate and burst", () => {
    template.hasResourceProperties("AWS::ApiGateway::Stage", {
      MethodSettings: Match.arrayWith([
        Match.objectLike({
          HttpMethod: "*",
          ResourcePath: "/*",
          ThrottlingRateLimit: 20,
          ThrottlingBurstLimit: 40,
        }),
      ]),
    });
  });

  test("the ingestion consumer reserves the configured count of concurrent executions", () => {
    template.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: "CcfIngestionConsumerLambda",
      ReservedConcurrentExecutions: SERVICE_SETTINGS.ingestionQueue.consumerReservedConcurrency,
    });
  });

  test("the request-driven Lambdas reserve no concurrency", () => {
    for (const functionName of ["CcfApiLambda", "CcfScheduledStartLambda"])
      template.hasResourceProperties("AWS::Lambda::Function", {
        FunctionName: functionName,
        ReservedConcurrentExecutions: Match.absent(),
      });
  });

  test("has the six tables with their keys, indexes, and TTLs", () => {
    const keys = (partition: string, sort?: string): JsonObject[] => [
      { AttributeName: partition, KeyType: "HASH" },
      ...(sort === undefined ? [] : [{ AttributeName: sort, KeyType: "RANGE" }]),
    ];
    template.resourceCountIs("AWS::DynamoDB::Table", 6);
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "CcfDestinations",
      KeySchema: keys("tenant_arn_prefix", "destination_id"),
      GlobalSecondaryIndexes: [
        {
          IndexName: "ByName",
          KeySchema: keys("tenant_arn_prefix", "name"),
          Projection: {
            ProjectionType: "INCLUDE",
            NonKeyAttributes: ["arn", "type", "status", "created_at", "updated_at"],
          },
        },
      ],
    });
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "CcfCustomConnectors",
      KeySchema: keys("tenant_arn_prefix", "destination_connector_key"),
      GlobalSecondaryIndexes: [
        {
          IndexName: "ByName",
          KeySchema: keys("tenant_arn_prefix", "destination_name_key"),
          Projection: {
            ProjectionType: "INCLUDE",
            NonKeyAttributes: [
              "connector_id",
              "arn",
              "destination_id",
              "name",
              "description",
              "status",
              "created_at",
              "updated_at",
            ],
          },
        },
      ],
    });
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "CcfCustomConnectorJobs",
      KeySchema: keys("tenant_arn_prefix", "job_id"),
      GlobalSecondaryIndexes: [
        {
          IndexName: "ByConnectorSummary",
          KeySchema: keys("connector_key", "created_at"),
          Projection: {
            ProjectionType: "INCLUDE",
            NonKeyAttributes: ["arn", "status", "updated_at"],
          },
        },
      ],
      StreamSpecification: Match.absent(),
      TimeToLiveSpecification: { AttributeName: "ttl", Enabled: true },
    });
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "CcfCollections",
      KeySchema: keys("connector_arn", "collection_id"),
      GlobalSecondaryIndexes: Match.absent(),
    });
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "CcfDocuments",
      KeySchema: keys("connector_arn", "collection_document_key"),
      GlobalSecondaryIndexes: Match.absent(),
    });
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "CcfIdempotency",
      KeySchema: keys("id"),
      GlobalSecondaryIndexes: Match.absent(),
      TimeToLiveSpecification: { AttributeName: "expiration", Enabled: true },
    });
    for (const table of Object.values(template.findResources("AWS::DynamoDB::Table")))
      expect(table.Properties).toMatchObject({
        BillingMode: "PAY_PER_REQUEST",
        SSESpecification: { SSEEnabled: true, SSEType: "KMS" },
        PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
      });
  });

  test("names every framework role under /ccf/", () => {
    for (const roleName of [
      "CcfApiRole",
      "CcfDestinationWorkflowRole",
      "CcfConnectorDeletionWorkflowRole",
      "CcfIngestionConsumerRole",
      "CcfBatchExecutionRole",
    ])
      template.hasResourceProperties("AWS::IAM::Role", { RoleName: roleName, Path: "/ccf/" });
  });

  test("CcfApiRole holds the statements and nothing broader for those actions", () => {
    // The form grantExecution writes: the executions of the machine its ARN's name segment names.
    const executions = (machine: string): unknown =>
      resolved(
        stack.formatArn({
          service: "states",
          resource: "execution",
          arnFormat: ArnFormat.COLON_RESOURCE_NAME,
          resourceName: `${Arn.split(machine, ArnFormat.COLON_RESOURCE_NAME).resourceName}:*`,
        }),
      );
    expectExactly("CcfApiRole", [
      {
        Action: ["states:StartExecution"],
        Resource: [
          stateMachineRef("CcfDestinationWorkflow"),
          stateMachineRef("CcfConnectorDeletionWorkflow"),
          stateMachineRef("CcfJobExecution"),
        ],
      },
      {
        Action: ["states:DescribeExecution"],
        Resource: [
          executions(stack.destinationWorkflows.creation.stateMachineArn),
          executions(stack.destinationWorkflows.deletion.stateMachineArn),
          executions(stack.connectorDeletionWorkflow.stateMachine.stateMachineArn),
        ],
      },
      {
        Action: ["states:DescribeExecution", "states:StopExecution"],
        Resource: [executions(stack.jobDispatch.stateMachine.stateMachineArn)],
      },
      {
        Action: ["batch:RegisterJobDefinition", "batch:TagResource"],
        Resource: [
          resolved(
            `arn:${Aws.PARTITION}:batch:${Aws.REGION}:${Aws.ACCOUNT_ID}:job-definition/CcfJob-*`,
          ),
        ],
      },
      { Action: ["sts:AssumeRole"], Resource: frameworkDestinationRoles },
      {
        Action: ["iam:TagRole", "iam:UntagRole"],
        Resource: [
          resolved(
            `arn:${Aws.PARTITION}:iam::${Aws.ACCOUNT_ID}:role/ccf-runtime/CcfConnectorRuntime-*`,
          ),
        ],
      },
    ]);
    // The schedule lifecycle falls back between create and update on their errors and never reads a schedule.
    expect(roleStatements("CcfApiRole").flatMap((statement) => statement.Action)).not.toContain(
      "scheduler:GetSchedule",
    );
  });

  test("CcfConnectorDeletionWorkflowRole is under /ccf/; its own test lists its statements", () => {
    template.hasResourceProperties("AWS::IAM::Role", {
      RoleName: "CcfConnectorDeletionWorkflowRole",
      Path: "/ccf/",
    });
  });

  test("CcfIngestionConsumerRole holds the statements and nothing broader for those actions", () => {
    const destinations = tableArn("CcfDestinations");
    expectExactly("CcfIngestionConsumerRole", [
      { Action: ["sts:AssumeRole"], Resource: frameworkDestinationRoles },
      {
        // Table.grant also names the table's indexes.
        Action: ["dynamodb:GetItem", "dynamodb:UpdateItem"],
        Resource: [destinations, { "Fn::Join": ["", [destinations, "/index/*"]] }],
      },
      {
        Action: [
          "sqs:ReceiveMessage",
          "sqs:ChangeMessageVisibility",
          "sqs:GetQueueUrl",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes",
        ],
        Resource: [queueArn("CcfIngestionQueue")],
      },
      { Action: ["sqs:SendMessage"], Resource: [queueArn("CcfIngestionDlq")] },
      { Action: ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey"], Resource: [keyArn()] },
      {
        Action: ["cloudwatch:PutMetricData"],
        Resource: ["*"],
        Condition: { StringEquals: { "cloudwatch:namespace": "CustomConnectorFramework" } },
      },
    ]);
  });

  test("the ingestion consumer sends the CcfDestinationWorkflow ARN as the external ID", () => {
    const variables = environment("CcfIngestionConsumerLambda");
    expect(variables.WORKFLOWS__DESTINATION).toEqual(
      resolved(
        `arn:${Aws.PARTITION}:states:${Aws.REGION}:${Aws.ACCOUNT_ID}:stateMachine:CcfDestinationWorkflow`,
      ),
    );
    template.hasResourceProperties("AWS::StepFunctions::StateMachine", {
      StateMachineName: "CcfDestinationWorkflow",
    });
  });

  test("the runtime role boundary is the document and allows nothing else", () => {
    const api = `arn:${Aws.PARTITION}:execute-api:${Aws.REGION}:${Aws.ACCOUNT_ID}:*/*`;
    const destination = "api/v1/destinations/${aws:PrincipalTag/ccf:destination-id}";
    const connector = `${destination}/custom-connectors/\${aws:PrincipalTag/ccf:connector-id}`;
    const [boundary] = Object.values(
      template.findResources("AWS::IAM::ManagedPolicy", {
        Properties: { ManagedPolicyName: "CcfConnectorRuntimeBoundary" },
      }),
    );
    expect(normalized(boundary.Properties.PolicyDocument)).toEqual(
      normalized({
        Version: "2012-10-17",
        Statement: [
          {
            Sid: "OwnConnectorRoutes",
            Effect: "Allow",
            Action: "execute-api:Invoke",
            Resource: [
              resolved(`${api}/GET/${destination}`),
              resolved(`${api}/GET/${connector}`),
              resolved(`${api}/*/${connector}/*`),
            ],
          },
          {
            Sid: "OwnTenantRoles",
            Effect: "Allow",
            Action: "sts:AssumeRole",
            Resource: [
              arn("iam::*:role/${aws:PrincipalTag/ccf:source-access-role}"),
              arn("iam::*:role/${aws:PrincipalTag/ccf:log-delivery-role}"),
            ],
            Condition: {
              StringEquals: { "aws:ResourceAccount": "${aws:PrincipalTag/ccf:tenant-account}" },
            },
          },
        ],
      }),
    );
  });

  test("no framework role holds an s3: or bedrock: action", () => {
    const actions = [
      ...Object.values(template.findResources("AWS::IAM::Policy")).flatMap(
        (policy) => policy.Properties.PolicyDocument.Statement,
      ),
      ...Object.values(template.findResources("AWS::IAM::Role")).flatMap((role) =>
        (
          (role.Properties.Policies as
            { PolicyDocument: { Statement: JsonObject[] } }[] | undefined) ?? []
        ).flatMap((policy) => policy.PolicyDocument.Statement),
      ),
      ...Object.values(template.findResources("AWS::IAM::ManagedPolicy")).flatMap(
        (policy) => policy.Properties.PolicyDocument.Statement,
      ),
    ].flatMap((statement: JsonObject) => asArray(statement.Action) as string[]);
    expect(
      actions.filter((action) => action.startsWith("s3:") || action.startsWith("bedrock:")),
    ).toEqual([]);
  });

  test("the job VPC has exactly one endpoint: S3 on the private route tables with the policy", () => {
    template.resourceCountIs("AWS::EC2::VPCEndpoint", 1);
    const [endpoint] = Object.values(template.findResources("AWS::EC2::VPCEndpoint")).map(
      (resource) => resource.Properties,
    );
    expect(endpoint.VpcEndpointType).toBe("Gateway");
    expect(endpoint.ServiceName).toEqual({
      "Fn::Join": ["", ["com.amazonaws.", { Ref: "AWS::Region" }, ".s3"]],
    });
    const privateRouteTables = Object.entries(template.findResources("AWS::EC2::RouteTable"))
      .filter(([id]) => id.includes("Private"))
      .map(([id]) => ({ Ref: id }));
    expect(privateRouteTables).toHaveLength(2);
    expect(endpoint.RouteTableIds).toEqual(privateRouteTables);
    expect(endpoint.PolicyDocument).toEqual({
      Version: "2012-10-17",
      Statement: [
        {
          Sid: "DestinationBuckets",
          Effect: "Allow",
          Principal: "*",
          Action: "s3:*",
          Resource: [arn("s3:::ccf-dst-*"), arn("s3:::ccf-dst-*/*")],
        },
        {
          Sid: "OtherAccountBuckets",
          Effect: "Allow",
          Principal: "*",
          Action: "s3:*",
          Resource: [arn("s3:::*"), arn("s3:::*/*")],
          Condition: { StringNotEquals: { "aws:ResourceAccount": { Ref: "AWS::AccountId" } } },
        },
      ],
    });
    expect(JSON.stringify(endpoint.PolicyDocument)).not.toContain("arn:aws:");
  });

  test("given the job subnets, when synthesized, then their network ACL allows all outbound and only VPC traffic and TCP replies inbound", () => {
    template.resourceCountIs("AWS::EC2::NetworkAcl", 1);
    template.resourceCountIs("AWS::EC2::SubnetNetworkAclAssociation", 2);
    const entries = Object.values(template.findResources("AWS::EC2::NetworkAclEntry")).map(
      (resource) => {
        const { Egress, RuleNumber, Protocol, PortRange, CidrBlock } = resource.Properties;
        return { Egress, RuleNumber, Protocol, PortRange, CidrBlock };
      },
    );
    expect(entries).toEqual([
      { Egress: true, RuleNumber: 100, Protocol: -1, PortRange: undefined, CidrBlock: "0.0.0.0/0" },
      {
        Egress: false,
        RuleNumber: 110,
        Protocol: -1,
        PortRange: undefined,
        CidrBlock: { "Fn::GetAtt": [expect.stringMatching(/Vpc/), "CidrBlock"] },
      },
      {
        Egress: false,
        RuleNumber: 120,
        Protocol: 6,
        PortRange: { From: 1024, To: 65535 },
        CidrBlock: "0.0.0.0/0",
      },
    ]);
  });

  test("the compute environment runs in the private subnets with only CcfJobSecurityGroup", () => {
    const securityGroupId = logicalId("AWS::EC2::SecurityGroup", {
      GroupName: "CcfJobSecurityGroup",
    });
    const group = json.Resources[securityGroupId].Properties;
    expect(group.SecurityGroupIngress).toBeUndefined();
    expect(group.SecurityGroupEgress).toEqual([
      {
        CidrIp: "0.0.0.0/0",
        Description: "Allow all outbound traffic by default",
        IpProtocol: "-1",
      },
    ]);
    template.resourceCountIs("AWS::EC2::SecurityGroupIngress", 0);
    const [environmentResource] = Object.values(
      template.findResources("AWS::Batch::ComputeEnvironment"),
    );
    const resources = (
      environmentResource.Properties as {
        ComputeResources: { SecurityGroupIds: unknown[]; Subnets: { Ref: string }[] };
      }
    ).ComputeResources;
    expect(resources.SecurityGroupIds).toEqual([{ "Fn::GetAtt": [securityGroupId, "GroupId"] }]);
    expect(
      resources.Subnets.map((subnet) => subnet.Ref).every((id) => id.includes("Private")),
    ).toBe(true);
    for (const subnet of resources.Subnets) {
      const [association] = Object.values(
        template.findResources("AWS::EC2::SubnetRouteTableAssociation", {
          Properties: { SubnetId: subnet },
        }),
      );
      const routes = Object.values(
        template.findResources("AWS::EC2::Route", {
          Properties: { RouteTableId: association.Properties.RouteTableId },
        }),
      );
      expect(
        routes.some(
          (route) =>
            route.Properties.DestinationCidrBlock === "0.0.0.0/0" &&
            route.Properties.NatGatewayId !== undefined,
        ),
      ).toBe(true);
    }
  });

  test("runs jobs in CcfJobExecution, with no job Lambda, stream mapping, or Batch state-change rule", () => {
    template.resourceCountIs("AWS::Lambda::EventSourceMapping", 1);
    template.resourceCountIs("AWS::Events::Rule", 0);
    expect(functions.map((properties) => properties.FunctionName)).not.toEqual(
      expect.arrayContaining([expect.stringMatching(/Job(Orchestrator|Status)/)]),
    );
    template.hasResourceProperties("AWS::StepFunctions::StateMachine", {
      StateMachineName: "CcfJobExecution",
      RoleArn: {
        "Fn::GetAtt": [logicalId("AWS::IAM::Role", { RoleName: "CcfJobExecutionRole" }), "Arn"],
      },
    });
  });

  test("the job execution role may update both tables its finishing transaction writes, and run .sync", () => {
    const roleId = logicalId("AWS::IAM::Role", { RoleName: "CcfJobExecutionRole" });
    const statements = Object.values(template.findResources("AWS::IAM::Policy"))
      .filter((policy) =>
        (policy.Properties.Roles as { Ref: string }[]).some((role) => role.Ref === roleId),
      )
      .flatMap((policy) => policy.Properties.PolicyDocument.Statement as JsonObject[])
      .filter((statement) => statement.Effect === "Allow");
    const allows = (action: string, resource: unknown): boolean =>
      statements.some(
        (statement) =>
          asArray(statement.Action).includes(action) &&
          asArray(statement.Resource).some((held) => same(held, resource)),
      );

    expect(allows("dynamodb:UpdateItem", tableArn("CcfCustomConnectors"))).toBe(true);
    expect(allows("dynamodb:UpdateItem", tableArn("CcfCustomConnectorJobs"))).toBe(true);
    expect(allows("batch:DescribeJobs", "*")).toBe(true);
    expect(
      statements.some((statement) => asArray(statement.Action).includes("events:PutRule")),
    ).toBe(true);
  });

  test("the scheduled-start role only reads the destinations table", () => {
    const [scheduledStart] = Object.values(
      template.findResources("AWS::Lambda::Function", {
        Properties: { FunctionName: "CcfScheduledStartLambda" },
      }),
    );
    const [roleId] = (scheduledStart.Properties.Role as { "Fn::GetAtt": string[] })["Fn::GetAtt"];
    const destinationActions = Object.values(template.findResources("AWS::IAM::Policy"))
      .filter((policy) => JSON.stringify(policy.Properties.Roles).includes(roleId))
      .flatMap((policy) => policy.Properties.PolicyDocument.Statement as JsonObject[])
      .filter((statement) =>
        JSON.stringify(statement.Resource).includes(tableArnId("CcfDestinations")),
      )
      .flatMap((statement) => asArray(statement.Action).map(String));

    expect(destinationActions.length).toBeGreaterThan(0);
    expect(destinationActions.filter((action) => /Put|Update|Delete|Write/.test(action))).toEqual(
      [],
    );
  });

  test("the log router image in the service settings has a version tag", () => {
    expect(environment("CcfApiLambda").JOB_DEFINITIONS__LOG_ROUTER_IMAGE).toBe(
      SERVICE_SETTINGS.logRouterImage,
    );
    expect(SERVICE_SETTINGS.logRouterImage).toMatch(
      /^public\.ecr\.aws\/aws-observability\/aws-for-fluent-bit:\d+\.\d+\.\d+(\.\d+)?$/,
    );
  });

  test("the API resource policy admits exactly the service account and each allowed tenant account", () => {
    const [api] = Object.values(template.findResources("AWS::ApiGateway::RestApi")).map(
      (resource) => resource.Properties,
    );
    const root = (account: unknown): unknown => ({
      AWS: resolved(`arn:${Aws.PARTITION}:iam::${account as string}:root`),
    });
    expect(api.Policy).toEqual({
      Version: "2012-10-17",
      Statement: [
        {
          Sid: "ServiceAndTenantAccounts",
          Effect: "Allow",
          Principal: {
            AWS: [
              (root(Aws.ACCOUNT_ID) as JsonObject).AWS,
              (root(TENANT_ACCOUNT) as JsonObject).AWS,
            ],
          },
          Action: "execute-api:Invoke",
          Resource: resolved(
            `arn:${Aws.PARTITION}:execute-api:${Aws.REGION}:${Aws.ACCOUNT_ID}:*/*/*/api/v1/destinations*`,
          ),
        },
      ],
    });
    expect(config.allowedTenantAccountIds).toEqual([TENANT_ACCOUNT]);
  });

  test("the API body names the API Lambda by name, so the Lambda can carry the REST API ID", () => {
    const [api] = Object.values(template.findResources("AWS::ApiGateway::RestApi")).map(
      (resource) => resource.Properties,
    );
    const body = JSON.stringify(api.Body);
    expect(body).toContain(":function:CcfApiLambda/invocations");
    const lambdaId = Object.keys(
      template.findResources("AWS::Lambda::Function", {
        Properties: { FunctionName: "CcfApiLambda" },
      }),
    )[0];
    expect(body).not.toContain(lambdaId);
    const variables = environment("CcfApiLambda");
    expect(variables.RUNTIME_ROLES__REST_API_ID).toEqual({
      Ref: Object.keys(template.findResources("AWS::ApiGateway::RestApi"))[0],
    });
    expect(variables.RUNTIME_ROLES__STAGE_NAME).toBe("ccf");
    expect(String(variables.RUNTIME_ROLES__ROUTES)).toMatch(
      /^\[\{"method":"GET","path":"\/api\/v1\/destinations\/\{destination_id\}/,
    );
  });

  test("API Gateway rejects a repeated document ID or collection in a batch delete body", () => {
    const [api] = Object.values(template.findResources("AWS::ApiGateway::RestApi")).map(
      (resource) => resource.Properties,
    );
    const body: unknown = api.Body;

    expect(body).toMatchObject({
      components: {
        schemas: {
          BatchDeleteCustomConnectorDocumentsRequestContent: {
            properties: { document_ids: { uniqueItems: true } },
          },
          BatchDeleteCustomConnectorCollectionsRequestContent: {
            properties: { collections: { uniqueItems: true } },
          },
        },
      },
    });
  });

  test("a runtime role may get and put its checkpoint, and never delete it", () => {
    const checkpoint =
      "/api/v1/destinations/{destination_id}/custom-connectors/{connector_id}/checkpoint";
    const routes: unknown = JSON.parse(String(environment("CcfApiLambda").RUNTIME_ROLES__ROUTES));

    expect(routes).toEqual(
      expect.arrayContaining([
        { method: "GET", path: checkpoint },
        { method: "PUT", path: checkpoint },
      ]),
    );
    expect(routes).not.toEqual(expect.arrayContaining([{ method: "DELETE", path: checkpoint }]));
  });

  test("the API and scheduled-start Lambdas get the idempotency expiry and scheduler retries from the service settings", () => {
    const api = environment("CcfApiLambda");
    expect(api.IDEMPOTENCY__EXPIRY_SECONDS).toBe(String(SERVICE_SETTINGS.idempotencyExpirySeconds));
    expect(api.SCHEDULER__RETRY_ATTEMPTS).toBe(String(SERVICE_SETTINGS.schedulerRetryAttempts));
    expect(api.SCHEDULER__MAX_EVENT_AGE_SECONDS).toBe(
      String(SERVICE_SETTINGS.schedulerMaxEventAgeSeconds),
    );
    expect(environment("CcfScheduledStartLambda").IDEMPOTENCY__EXPIRY_SECONDS).toBe(
      String(SERVICE_SETTINGS.idempotencyExpirySeconds),
    );
  });

  test("every Lambda gets exactly the settings its composition root reads, and no Powertools service name", () => {
    const contract = settingsOf();
    const expected: Record<string, readonly string[]> = {
      CcfApiLambda: contract.api_handler,
      CcfScheduledStartLambda: contract.scheduled_start_handler,
      CcfIngestionConsumerLambda: contract.ingestion_consumer_handler,
    };
    for (const [functionName, names] of Object.entries(expected))
      expect({ functionName, names: Object.keys(environment(functionName)).sort() }).toEqual({
        functionName,
        names: [...names].sort(),
      });
    expect(environment("CcfApiLambda").ALLOWED_TENANT_ACCOUNT_IDS).toBe(TENANT_ACCOUNT);
  });

  test("CcfBatchExecutionRole holds exactly the three statements", () => {
    template.hasResourceProperties("AWS::IAM::Role", {
      RoleName: "CcfBatchExecutionRole",
      Path: "/ccf/",
      AssumeRolePolicyDocument: {
        Statement: [
          {
            Action: "sts:AssumeRole",
            Effect: "Allow",
            Principal: { Service: "ecs-tasks.amazonaws.com" },
          },
        ],
      },
    });
    const repositories = [Aws.ACCOUNT_ID, TENANT_ACCOUNT].map((account) =>
      resolved(`arn:${Aws.PARTITION}:ecr:${Aws.REGION}:${account}:repository/ccf-connectors/*`),
    );
    expect(
      roleStatements("CcfBatchExecutionRole").map((statement) => ({
        ...statement,
        Action: [...statement.Action].sort(),
      })),
    ).toEqual([
      { Action: ["ecr:GetAuthorizationToken"], Resource: ["*"] },
      {
        Action: [
          "ecr:BatchCheckLayerAvailability",
          "ecr:BatchGetImage",
          "ecr:GetDownloadUrlForLayer",
        ],
        Resource: repositories,
      },
      {
        Action: ["logs:CreateLogStream", "logs:PutLogEvents"],
        Resource: [
          resolved(
            `arn:${Aws.PARTITION}:logs:${Aws.REGION}:${Aws.ACCOUNT_ID}:log-group:/aws/batch/job:log-stream:ccf-log-router/*`,
          ),
        ],
      },
    ]);
  });

  test("creates no bucket, no knowledge base, and no image repository", () => {
    const types = Object.values(json.Resources).map((resource) => resource.Type);
    expect(
      types.filter(
        (type) =>
          type === "AWS::S3::Bucket" ||
          type === "AWS::ECR::Repository" ||
          type.startsWith("AWS::Bedrock::"),
      ),
    ).toEqual([]);
  });

  test("outputs and exports the four tenant values and the names the Ops stack reads", () => {
    const outputs = Object.entries(json.Outputs)
      .filter(([, output]) => output.Export !== undefined)
      .map(([id, output]) => [id, (output.Export as JsonObject).Name]);
    for (const [id, name] of [
      ["ApiEndpoint", FrameworkExport.API_ENDPOINT],
      ["ServiceAccountId", FrameworkExport.SERVICE_ACCOUNT_ID],
      ["BatchExecutionRoleArn", FrameworkExport.BATCH_EXECUTION_ROLE_ARN],
      ["DestinationWorkflowArn", FrameworkExport.DESTINATION_WORKFLOW_ARN],
    ])
      expect(outputs).toContainEqual([id, name]);
    const exported = outputs.map(([, name]) => name);
    for (const name of Object.values(FrameworkExport).filter(
      (value) => value !== FrameworkExport.ALARM_TOPIC_ARN,
    ))
      expect(exported).toContain(name);
  });

  test("the ingestion queue sends to its DLQ after the configured receives and the consumer takes one message with partial failures", () => {
    template.hasResourceProperties("AWS::SQS::Queue", {
      QueueName: "CcfIngestionQueue",
      RedrivePolicy: {
        deadLetterTargetArn: queueArn("CcfIngestionDlq"),
        maxReceiveCount: SERVICE_SETTINGS.ingestionQueue.maxReceiveCount,
      },
    });
    template.hasResourceProperties("AWS::Lambda::EventSourceMapping", {
      EventSourceArn: queueArn("CcfIngestionQueue"),
      BatchSize: 1,
      FunctionResponseTypes: ["ReportBatchItemFailures"],
    });
  });

  test("keeps every log group for the configured retention", () => {
    for (const group of Object.values(template.findResources("AWS::Logs::LogGroup")))
      expect(group.Properties.RetentionInDays).toBe(7);
  });

  test("runs every function on arm64", () => {
    expect(functions.length).toBeGreaterThan(0);
    for (const properties of functions) expect(properties.Architectures).toEqual(["arm64"]);
  });

  test("wires the three state machines with their roles", () => {
    for (const [machine, role] of [
      ["CcfDestinationWorkflow", "CcfDestinationWorkflowRole"],
      ["CcfDestinationDeletionWorkflow", "CcfDestinationWorkflowRole"],
      ["CcfConnectorDeletionWorkflow", "CcfConnectorDeletionWorkflowRole"],
    ])
      template.hasResourceProperties("AWS::StepFunctions::StateMachine", {
        StateMachineName: machine,
        RoleArn: { "Fn::GetAtt": [logicalId("AWS::IAM::Role", { RoleName: role }), "Arn"] },
      });
  });

  test("every role has CcfServiceRoleBoundary, which allows only the services the framework calls", () => {
    const boundaryId = logicalId("AWS::IAM::ManagedPolicy", {
      ManagedPolicyName: "CcfServiceRoleBoundary",
    });
    expect(json.Resources[boundaryId].Properties).toMatchObject({
      Path: "/ccf/",
      PolicyDocument: {
        Statement: [
          {
            Sid: "ServicesTheFrameworkCalls",
            Effect: "Allow",
            Action: [
              "batch:*",
              "cloudwatch:*",
              "dynamodb:*",
              "ecr:*",
              "events:*",
              "execute-api:*",
              "iam:*",
              "kms:*",
              "lambda:*",
              "logs:*",
              "scheduler:*",
              "sqs:*",
              "states:*",
              "sts:*",
              "xray:*",
            ],
            Resource: "*",
          },
        ],
      },
    });
    const roles = Object.values(json.Resources).filter(
      (resource) => resource.Type === "AWS::IAM::Role",
    );
    expect(roles.length).toBeGreaterThan(0);
    for (const role of roles)
      expect(role.Properties.PermissionsBoundary).toEqual({ Ref: boundaryId });
  });

  test("no role in the stack has an action outside its boundary's services", () => {
    const allowed = new Set<string>(SERVICE_ROLE_SERVICES);
    interface Statement {
      readonly Action: string | readonly string[];
    }
    interface Document {
      readonly Statement: readonly Statement[];
    }
    const documents: Document[] = [];
    for (const resource of Object.values(json.Resources)) {
      if (resource.Type === "AWS::IAM::Policy")
        documents.push(resource.Properties.PolicyDocument as Document);
      if (resource.Type === "AWS::IAM::Role")
        for (const inline of (resource.Properties.Policies ?? []) as { PolicyDocument: Document }[])
          documents.push(inline.PolicyDocument);
    }
    const services = documents
      .flatMap((document) => document.Statement)
      .flatMap((statement) => [statement.Action].flat())
      .map((action) => action.split(":")[0]);
    expect(services.length).toBeGreaterThan(0);
    for (const service of services) expect(allowed).toContain(service);
  });
});
