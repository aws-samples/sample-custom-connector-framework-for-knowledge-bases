import { App, Duration, RemovalPolicy, Stack } from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { Role, ServicePrincipal } from "aws-cdk-lib/aws-iam";
import { RetentionDays } from "aws-cdk-lib/aws-logs";
import {
  CustomState,
  DefinitionBody,
  IChainable,
  Pass,
  StateMachine,
  Succeed,
} from "aws-cdk-lib/aws-stepfunctions";
import { DynamoAttributeValue, DynamoUpdateItem } from "aws-cdk-lib/aws-stepfunctions-tasks";
import { Table, AttributeType } from "aws-cdk-lib/aws-dynamodb";
import {
  OBJECT_KEYS_PER_PAGE,
  RETRY_POLICY,
  SdkService,
  TASK_TIMEOUT_ERROR,
  childExecution,
  continueAsNew,
  continuationsSpent,
  executionInput,
  jsonataStateMachine,
  objectDeletionLoop,
  sdkCall,
  startPassCount,
  withRetry,
} from "../lib/construct-groups/workflow-states";

type JsonObject = Record<string, unknown>;

const TENANT_ROLES = ["arn:aws:iam::111122223333:role/ccf/destinations/CcfDestinationRole-*"];
const TENANT = { roleArn: "{% $states.input.role_arn %}", roleArns: TENANT_ROLES };
const MACHINE_ARN = "arn:aws:states:us-east-1:444455556666:stateMachine:Example";
const POLICY = {
  IntervalSeconds: RETRY_POLICY.interval.toSeconds(),
  MaxAttempts: RETRY_POLICY.maxAttempts,
  BackoffRate: RETRY_POLICY.backoffRate,
  JitterStrategy: "FULL",
};

interface Built {
  readonly states: Record<string, JsonObject>;
  readonly definition: JsonObject;
  readonly statements: JsonObject[];
}

/** Build one machine from the chain `build` returns and read its definition and its role's statements. */
const synthesize = (build: (stack: Stack) => IChainable): Built => {
  const stack = new Stack(new App(), "Workflow", {
    env: { account: "444455556666", region: "us-east-1" },
  });
  const role = new Role(stack, "Role", { assumedBy: new ServicePrincipal("states.amazonaws.com") });
  jsonataStateMachine(stack, "Machine", {
    stateMachineName: "Example",
    definition: build(stack),
    role,
    logRetention: RetentionDays.ONE_WEEK,
    removalPolicy: RemovalPolicy.DESTROY,
  });
  const template = Template.fromStack(stack);
  const [machine] = Object.values(
    template.findResources("AWS::StepFunctions::StateMachine", {
      Properties: { StateMachineName: "Example" },
    }),
  );
  const body = (machine.Properties as { DefinitionString: unknown }).DefinitionString;
  const definition = JSON.parse(
    typeof body === "string"
      ? body
      : (stack.resolve(body) as { "Fn::Join": [string, unknown[]] })["Fn::Join"][1]
          .map((part) => (typeof part === "string" ? part : "TOKEN"))
          .join(""),
  ) as JsonObject;
  const statements = Object.values(template.findResources("AWS::IAM::Policy")).flatMap(
    (policy) =>
      (policy.Properties as { PolicyDocument: { Statement: JsonObject[] } }).PolicyDocument
        .Statement,
  );
  return { states: definition.States as Record<string, JsonObject>, definition, statements };
};

describe("the JSONata workflow builders", () => {
  it("builds a JSONata machine", () => {
    const { definition } = synthesize((stack) => new Succeed(stack, "Done"));

    expect(definition.QueryLanguage).toBe("JSONata");
  });

  it("gives an sdkCall the one retry policy on its service's errors and States.Timeout", () => {
    const { states } = synthesize((stack) =>
      sdkCall(stack, "GetItem", {
        service: SdkService.DYNAMODB,
        action: "getItem",
        iamResources: ["arn:aws:dynamodb:us-east-1:444455556666:table/T"],
        arguments: { TableName: "T", Key: { id: { S: executionInput("id") } } },
      }),
    );

    const [retry] = states.GetItem.Retry as JsonObject[];
    expect(retry).toEqual({
      ErrorEquals: expect.arrayContaining(["DynamoDb.ThrottlingException", TASK_TIMEOUT_ERROR]),
      ...POLICY,
    });
    expect((states.GetItem.Arguments as JsonObject).Key).toEqual({
      id: { S: "{% $states.context.Execution.Input.id %}" },
    });
  });

  it("runs a tenant call as the tenant role and grants only sts:AssumeRole on the tenant roles", () => {
    const { states, statements } = synthesize((stack) =>
      sdkCall(stack, "ListBuckets", {
        service: SdkService.S3,
        action: "listBuckets",
        iamResources: ["*"],
        arguments: {},
        tenant: TENANT,
      }),
    );

    expect(states.ListBuckets.Credentials).toEqual({ RoleArn: TENANT.roleArn });
    const actions = statements.flatMap((statement) => [statement.Action].flat());
    expect(actions).not.toContain("s3:listBuckets");
    const assumes = statements.filter((statement) =>
      [statement.Action].flat().includes("sts:AssumeRole"),
    );
    expect(assumes.flatMap((statement) => [statement.Resource].flat())).toEqual(
      expect.arrayContaining(TENANT_ROLES),
    );
    expect(assumes.flatMap((statement) => [statement.Resource].flat())).not.toContain("*");
  });

  it("applies the policy and an additional retrier through withRetry on another task and on a CustomState", () => {
    const { states } = synthesize((stack) => {
      const table = new Table(stack, "Table", {
        partitionKey: { name: "id", type: AttributeType.STRING },
      });
      const update = withRetry(
        DynamoUpdateItem.jsonata(stack, "Update", {
          table,
          key: { id: DynamoAttributeValue.fromString("1") },
          updateExpression: "SET x = :x",
        }),
        {
          errors: ["DynamoDB.ConditionalCheckFailedException"],
          interval: Duration.seconds(1),
          maxAttempts: 2,
        },
      );
      const custom = withRetry(
        new CustomState(stack, "Custom", {
          stateJson: {
            Type: "Task",
            Resource: "arn:aws:states:::aws-sdk:sqs:sendMessage",
            Arguments: {},
          },
        }),
      );
      return update.next(custom);
    });

    expect(states.Update.Retry).toEqual([
      { ErrorEquals: [TASK_TIMEOUT_ERROR], ...POLICY },
      expect.objectContaining({
        ErrorEquals: ["DynamoDB.ConditionalCheckFailedException"],
        MaxAttempts: 2,
      }),
    ]);
    expect(states.Custom.Retry).toEqual([{ ErrorEquals: [TASK_TIMEOUT_ERROR], ...POLICY }]);
  });

  it("continues as new from the execution input with continuation one higher", () => {
    const { states } = synthesize((stack) =>
      continueAsNew(stack, "Continue", {
        executionName: "{% $states.context.Execution.Name & '-1' %}",
        stateMachineArn: MACHINE_ARN,
      }),
    );

    expect((states.Continue.Arguments as JsonObject).Input).toBe(
      "{% $string($merge([$states.context.Execution.Input, {'continuation': $states.context.Execution.Input.continuation + 1}])) %}",
    );
    expect(continuationsSpent()).toBeDefined();
  });

  it("starts a child execution and waits for it", () => {
    const { states } = synthesize((stack) => {
      const child = new StateMachine(stack, "Child", {
        definitionBody: DefinitionBody.fromChainable(new Succeed(stack, "ChildDone")),
      });
      return childExecution(stack, "RunChild", {
        stateMachine: child,
        input: { job_id: executionInput("job_id") },
        executionName: "{% 'x' %}",
      });
    });

    expect(String(states.RunChild.Resource)).toContain("states:startExecution.sync:2");
    expect((states.RunChild.Arguments as { Input: JsonObject }).Input).toEqual(
      expect.objectContaining({ job_id: "{% $states.context.Execution.Input.job_id %}" }),
    );
  });

  it("names the five object-deletion states, pages 32 keys, and counts each pass", () => {
    const { states } = synthesize((stack) => {
      const done = new Succeed(stack, "Done");
      return startPassCount(stack, "StartPasses").next(
        objectDeletionLoop(stack, {
          name: "Bucket",
          bucketName: "{% $states.input.bucket_name %}",
          tenant: TENANT,
          done,
          budgetSpent: new Pass(stack, "Budget"),
          onMissingBucket: new Pass(stack, "Missing"),
        }),
      );
    });

    expect(Object.keys(states)).toEqual(
      expect.arrayContaining([
        "ListBucketObjects",
        "HasBucketObjects",
        "DeleteBucketObjects",
        "CheckBucketObjectsDeleted",
        "BucketObjectsNotDeleted",
      ]),
    );
    expect((states.ListBucketObjects.Arguments as JsonObject).MaxKeys).toBe(OBJECT_KEYS_PER_PAGE);
    expect(states.ListBucketObjects.Assign).toEqual({ passes: "{% $passes + 1 %}" });
    expect(states.StartPasses.Assign).toEqual({ passes: 0 });
    expect(states.ListBucketObjects.Credentials).toEqual({ RoleArn: TENANT.roleArn });
  });
});
