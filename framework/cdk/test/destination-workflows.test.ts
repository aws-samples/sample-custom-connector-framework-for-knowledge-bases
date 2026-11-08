import { App, RemovalPolicy, Stack } from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { AttributeType, Table } from "aws-cdk-lib/aws-dynamodb";
import { Key } from "aws-cdk-lib/aws-kms";
import { RetentionDays } from "aws-cdk-lib/aws-logs";
import { SERVICE_SETTINGS } from "../lib/common/config";
import { DestinationWorkflows } from "../lib/construct-groups/destination-workflows";
import {
  TestStatement,
  isCdkLoggingOrTracing,
  partitionJoinArn,
  renderedDefinition,
  roleStatements,
} from "./state-machine-definition";

const SERVICE_ACCOUNT = "444455556666";
const TENANT_ACCOUNT = "111122223333";

const stack = new Stack(new App(), "Workflows", {
  env: { account: SERVICE_ACCOUNT, region: "us-west-2" },
});
const table = new Table(stack, "Destinations", {
  tableName: "CcfDestinations",
  partitionKey: { name: "tenant_arn_prefix", type: AttributeType.STRING },
  sortKey: { name: "destination_id", type: AttributeType.STRING },
});
const key = new Key(stack, "ServiceKey");
new DestinationWorkflows(stack, "DestinationWorkflow", {
  destinationsTable: table,
  serviceKey: key,
  serviceAccountId: SERVICE_ACCOUNT,
  allowedTenantAccountIds: [TENANT_ACCOUNT],
  region: "us-west-2",
  deletionProtectionThreshold: 15,
  settings: SERVICE_SETTINGS.destinationWorkflow,
  logRetention: RetentionDays.ONE_MONTH,
  removalPolicy: RemovalPolicy.DESTROY,
});
const template = Template.fromStack(stack);
const statements = roleStatements(template, "CcfDestinationWorkflowRole");
const logicalIdOf = (type: string): string => Object.keys(template.findResources(type))[0];
/** The ARN a fixed-name resource has when CDK builds it from the stack's literal Region and account. */
const literalArn = (rest: string): unknown =>
  partitionJoinArn(`:states:us-west-2:${SERVICE_ACCOUNT}:${rest}`);
/** The same ARN built from the Region and account pseudo parameters. */
const pseudoArn = (rest: string): unknown => ({
  "Fn::Join": [
    "",
    [
      "arn:",
      { Ref: "AWS::Partition" },
      ":states:",
      { Ref: "AWS::Region" },
      ":",
      { Ref: "AWS::AccountId" },
      `:${rest}`,
    ],
  ],
});
const withAction = (action: string): TestStatement[] =>
  statements.filter((statement) => statement.Action.includes(action));

describe("DestinationWorkflows' role", () => {
  test("assumes only destination roles, in the service account and each tenant account", () => {
    const resources = withAction("sts:AssumeRole").flatMap((statement) => statement.Resource);

    expect([...new Set(resources.map((resource) => JSON.stringify(resource)))].sort()).toEqual(
      [
        partitionJoinArn(`:iam::${SERVICE_ACCOUNT}:role/ccf/destinations/CcfDestinationRole-*`),
        partitionJoinArn(`:iam::${TENANT_ACCOUNT}:role/ccf/destinations/CcfDestinationRole-*`),
      ]
        .map((resource) => JSON.stringify(resource))
        .sort(),
    );
  });

  test("holds exactly its statements apart from logging and tracing", () => {
    const tableArn = [{ "Fn::GetAtt": [logicalIdOf("AWS::DynamoDB::Table"), "Arn"] }];
    const child = "stateMachine:CcfDestinationDeletionWorkflow";
    const own = statements
      .filter((statement) => !isCdkLoggingOrTracing(statement))
      .filter((statement) => !statement.Action.includes("sts:AssumeRole"));

    expect(own).toEqual([
      {
        Effect: "Allow",
        Action: ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey"],
        Resource: [{ "Fn::GetAtt": [logicalIdOf("AWS::KMS::Key"), "Arn"] }],
      },
      {
        Sid: "MetricsOnlyInTheFrameworkNamespace",
        Effect: "Deny",
        Action: ["cloudwatch:PutMetricData"],
        Resource: ["*"],
        Condition: { StringNotEquals: { "cloudwatch:namespace": "CustomConnectorFramework" } },
      },
      { Effect: "Allow", Action: ["dynamodb:updateItem"], Resource: tableArn },
      { Effect: "Allow", Action: ["dynamodb:getItem"], Resource: tableArn },
      { Effect: "Allow", Action: ["dynamodb:deleteItem"], Resource: tableArn },
      // The deletion's continue-as-new.
      { Effect: "Allow", Action: ["states:startExecution"], Resource: [pseudoArn(child)] },
      // The creation's .sync run of the deletion.
      { Effect: "Allow", Action: ["states:StartExecution"], Resource: [literalArn(child)] },
      {
        Effect: "Allow",
        Action: ["states:DescribeExecution", "states:StopExecution"],
        Resource: [literalArn("execution:CcfDestinationDeletionWorkflow*")],
      },
      {
        Effect: "Allow",
        Action: ["events:PutTargets", "events:PutRule", "events:DescribeRule"],
        Resource: [
          partitionJoinArn(
            `:events:us-west-2:${SERVICE_ACCOUNT}:rule/StepFunctionsGetEventsForStepFunctionsExecutionRule`,
          ),
        ],
      },
      { Effect: "Allow", Action: ["cloudwatch:putMetricData"], Resource: ["*"] },
    ]);
  });

  test("holds no s3:, iam:, or bedrock: action", () => {
    expect(
      statements
        .flatMap((statement) => statement.Action)
        .map(String)
        .filter((action) => /^(s3|iam|bedrock):/.test(action)),
    ).toEqual([]);
  });

  test("is trusted by Step Functions for both state machines in this account", () => {
    template.hasResourceProperties("AWS::IAM::Role", {
      RoleName: "CcfDestinationWorkflowRole",
      Path: "/ccf/",
      AssumeRolePolicyDocument: {
        Version: "2012-10-17",
        Statement: [
          {
            Effect: "Allow",
            Principal: { Service: "states.amazonaws.com" },
            Action: "sts:AssumeRole",
            Condition: {
              StringEquals: { "aws:SourceAccount": { Ref: "AWS::AccountId" } },
              ArnLike: {
                "aws:SourceArn": [
                  pseudoArn("stateMachine:CcfDestinationWorkflow"),
                  pseudoArn("stateMachine:CcfDestinationDeletionWorkflow"),
                ],
              },
            },
          },
        ],
      },
    });
  });
});

describe("DestinationWorkflows' state machines", () => {
  test.each(["CcfDestinationWorkflow", "CcfDestinationDeletionWorkflow"])(
    "%s is a Standard state machine that traces with X-Ray and logs errors with execution data",
    (name) => {
      template.hasResourceProperties("AWS::StepFunctions::StateMachine", {
        StateMachineName: name,
        StateMachineType: "STANDARD",
        TracingConfiguration: { Enabled: true },
        LoggingConfiguration: { Level: "ERROR", IncludeExecutionData: true },
      });
    },
  );

  test("the creation workflow runs the deletion workflow as a child", () => {
    const deleteDestination = renderedDefinition(template, "CcfDestinationWorkflow").States
      .DeleteDestination;

    expect(deleteDestination.Resource).toBe(
      "arn:${AWS::Partition}:states:::states:startExecution.sync:2",
    );
    expect(deleteDestination.Arguments?.StateMachineArn).toBe(
      `arn:\${AWS::Partition}:states:us-west-2:${SERVICE_ACCOUNT}:stateMachine:CcfDestinationDeletionWorkflow`,
    );
  });

  test("DestinationCreationFailed is published with no dimension", () => {
    expect(
      renderedDefinition(template, "CcfDestinationWorkflow").States.PublishCreationFailed.Arguments,
    ).toEqual({
      Namespace: "CustomConnectorFramework",
      MetricData: [{ MetricName: "DestinationCreationFailed", Value: 1, Unit: "Count" }],
    });
  });

  test("an unresolved service Region fails synthesis", () => {
    const unresolved = new Stack(new App(), "NoRegion", { env: { account: SERVICE_ACCOUNT } });
    expect(
      () =>
        new DestinationWorkflows(unresolved, "DestinationWorkflow", {
          destinationsTable: new Table(unresolved, "Destinations", {
            partitionKey: { name: "tenant_arn_prefix", type: AttributeType.STRING },
          }),
          serviceKey: new Key(unresolved, "ServiceKey"),
          serviceAccountId: SERVICE_ACCOUNT,
          allowedTenantAccountIds: [],
          region: unresolved.region,
          deletionProtectionThreshold: 15,
          settings: SERVICE_SETTINGS.destinationWorkflow,
          logRetention: RetentionDays.ONE_MONTH,
          removalPolicy: RemovalPolicy.DESTROY,
        }),
    ).toThrow("needs the service Region at synth");
  });
});
