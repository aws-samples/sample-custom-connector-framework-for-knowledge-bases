import { App, Duration, RemovalPolicy, Stack } from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { AttributeType, Table } from "aws-cdk-lib/aws-dynamodb";
import { Role, ServicePrincipal } from "aws-cdk-lib/aws-iam";
import { RetentionDays } from "aws-cdk-lib/aws-logs";
import { SERVICE_SETTINGS } from "../lib/common/config";
import { DestinationWorkflowName } from "../lib/common/destination-names";
import { destinationDeletionStates } from "../lib/construct-groups/destination-deletion-states";
import {
  OBJECTS_NOT_DELETED,
  OBJECT_KEYS_PER_PAGE,
  executionInput,
  jsonataStateMachine,
} from "../lib/construct-groups/workflow-states";
import { DestinationRole } from "../lib/constructs/destination-role";
import {
  literalFramework,
  propertiesOf,
  resourcesOf,
  statementsOf,
  tenantStack,
} from "./construct-fixtures";
import {
  AslTestDefinition,
  AslTestState,
  reachableFrom,
  renderedDefinition,
  structuralProblems,
  taskStates,
} from "./state-machine-definition";

const SETTINGS = {
  ...SERVICE_SETTINGS.destinationWorkflow,
  maxPolls: 7,
  dataSourceBusyRetry: { interval: Duration.seconds(11), maxAttempts: 4 },
};
const NOT_FOUND = {
  bedrock: "BedrockAgent.ResourceNotFoundException",
  iam: "Iam.NoSuchEntityException",
  s3: "S3.NoSuchBucketException",
};

const synthesize = (): AslTestDefinition => {
  const stack = new Stack(new App(), "Workflow", {
    env: { account: "444455556666", region: "us-east-1" },
  });
  const destinationsTable = new Table(stack, "Destinations", {
    partitionKey: { name: "tenant_arn_prefix", type: AttributeType.STRING },
    sortKey: { name: "destination_id", type: AttributeType.STRING },
  });
  jsonataStateMachine(stack, "Machine", {
    stateMachineName: DestinationWorkflowName.DELETION_STATE_MACHINE,
    definition: destinationDeletionStates(stack, {
      destinationsTable,
      destinationRoleArns: ["arn:aws:iam::111122223333:role/ccf/destinations/CcfDestinationRole-*"],
      settings: SETTINGS,
    }),
    role: new Role(stack, "Role", { assumedBy: new ServicePrincipal("states.amazonaws.com") }),
    logRetention: RetentionDays.ONE_WEEK,
    removalPolicy: RemovalPolicy.DESTROY,
  });
  return renderedDefinition(
    Template.fromStack(stack),
    DestinationWorkflowName.DELETION_STATE_MACHINE,
  );
};

/** Every action the destination role's own policy grants. */
const destinationRoleActions = (): string[] => {
  const stack = tenantStack();
  new DestinationRole(stack, "Role", {
    framework: literalFramework(),
    destinationName: "GitHub",
    removalPolicy: RemovalPolicy.DESTROY,
  });
  const [role] = resourcesOf(Template.fromStack(stack), "AWS::IAM::Role");
  return (propertiesOf(role).Policies as { PolicyDocument: unknown }[])
    .flatMap((policy) => statementsOf(policy.PolicyDocument))
    .flatMap((statement) => statement.Action as string[]);
};

const catchTarget = (state: AslTestState, error: string): string | undefined =>
  state.Catch?.find((caught) => caught.ErrorEquals.includes(error))?.Next;

/** A Task's SDK service and action, from `arn:<partition>:states:::aws-sdk:<service>:<action>`. */
const sdkTarget = (state: AslTestState): { service: string; action: string } => {
  const [, service, action] = /aws-sdk:(\w+):(\w+)$/.exec(String(state.Resource)) ?? [];
  return { service, action };
};

const choiceTargets = (state: AslTestState): string[] =>
  (state.Choices ?? []).map((choice) => choice.Next);

describe("destinationDeletionStates", () => {
  const definition = synthesize();
  const { States: states } = definition;
  const without = (name: string): AslTestDefinition => ({
    StartAt: definition.StartAt,
    States: Object.fromEntries(Object.entries(states).filter(([state]) => state !== name)),
  });

  it("has no repeated or out-of-scope state name", () => {
    expect(structuralProblems(definition)).toEqual([]);
  });

  it.each([
    ["ReadDataSource", NOT_FOUND.bedrock, "ReadKnowledgeBase"],
    ["DeleteDataSource", NOT_FOUND.bedrock, "ReadKnowledgeBase"],
    ["ListUnfinishedIngestionJobs", NOT_FOUND.bedrock, "ReadKnowledgeBase"],
    ["StopIngestionJob", NOT_FOUND.bedrock, "WaitForIngestionJobs"],
    ["ReadKnowledgeBase", NOT_FOUND.bedrock, "FindIngestionLogDelivery"],
    ["DeleteKnowledgeBase", NOT_FOUND.bedrock, "FindIngestionLogDelivery"],
    ["DeleteKnowledgeBaseRolePolicy", NOT_FOUND.iam, "DeleteKnowledgeBaseRole"],
    ["DeleteKnowledgeBaseRole", NOT_FOUND.iam, "ListBucketObjects"],
    ["ListBucketObjects", NOT_FOUND.s3, "DeleteDestinationItem"],
    ["DeleteBucket", NOT_FOUND.s3, "DeleteDestinationItem"],
  ])("sends %s's %s to %s, the next resource", (state, error, next) => {
    expect(catchTarget(states[state], error)).toBe(next);
  });

  it.each([
    ["CheckDataSourceDeletion", "DataSourceDeletionFailed"],
    ["CheckKnowledgeBaseDeletion", "KnowledgeBaseDeletionFailed"],
  ])("fails %s's resource left in another status with %s", (check, failure) => {
    expect(choiceTargets(states[check])).toContain(failure);
    expect(states[failure]).toMatchObject({ Type: "Fail", Error: failure });
  });

  it.each([
    ["CheckDataSourceDeletion", "WaitForDataSourceDeletion", "ReadDataSource"],
    ["CheckKnowledgeBaseDeletion", "WaitForKnowledgeBaseDeletion", "ReadKnowledgeBase"],
  ])("waits on a DELETING resource in %s with no poll limit", (check, wait, read) => {
    const toWait = states[check].Choices?.find((choice) => choice.Next === wait);
    expect(JSON.stringify(toWait)).toContain("DELETING");
    expect(states[wait].Next).toBe(read);
    expect(states[read].Next).toBe(check);
  });

  it("fails an S3 refusal with BucketObjectsNotDeleted", () => {
    expect(choiceTargets(states.CheckBucketObjectsDeleted)).toContain("BucketObjectsNotDeleted");
    expect(states.BucketObjectsNotDeleted).toMatchObject({
      Type: "Fail",
      Error: OBJECTS_NOT_DELETED,
    });
  });

  it("lists at most OBJECT_KEYS_PER_PAGE keys a page, keeping only KeyCount and Key", () => {
    expect(states.ListBucketObjects.Arguments?.MaxKeys).toBe(OBJECT_KEYS_PER_PAGE);
    expect(states.ListBucketObjects.Output).toContain("'KeyCount': $states.result.KeyCount");
    expect(states.ListBucketObjects.Output).toContain("{'Key': Key}");
  });

  it.each([
    "CheckDataSourceDeletion",
    "CheckKnowledgeBaseDeletion",
    "HasUnfinishedIngestionJobs",
    "HasBucketObjects",
  ])("sends %s to ContinueDeletion once the pass budget is spent", (loop) => {
    expect(choiceTargets(states[loop])).toContain("CheckContinuations");
    expect(states.CheckContinuations.Default).toBe("ContinueDeletion");
    expect(states.ContinueDeletion.Next).toBe("DeletionContinued");
  });

  it("reaches DeleteDataSource from ReadDataSource only through the ingestion check", () => {
    expect(reachableFrom(definition, "ReadDataSource")).toContain("DeleteDataSource");
    expect(reachableFrom(without("HasUnfinishedIngestionJobs"), "ReadDataSource")).not.toContain(
      "DeleteDataSource",
    );
  });

  it("retries DeleteDataSource's ValidationException with the configured interval and attempts", () => {
    expect(states.DeleteDataSource.Retry).toContainEqual(
      expect.objectContaining({
        ErrorEquals: ["BedrockAgent.ValidationException"],
        IntervalSeconds: 11,
        MaxAttempts: 4,
      }),
    );
  });

  it("gives up on unfinished ingestion jobs after the configured checks", () => {
    expect(choiceTargets(states.HasUnfinishedIngestionJobs)).toContain("IngestionJobsNotStopped");
    expect(JSON.stringify(states.HasUnfinishedIngestionJobs)).toContain(">= 7");
  });

  it("deletes the item last, and only while DELETING", () => {
    const item = states.DeleteDestinationItem;
    expect(item.End).toBe(true);
    expect(item.Arguments).toMatchObject({
      ConditionExpression: "#status = :deleting",
      ExpressionAttributeValues: { ":deleting": { S: "DELETING" } },
    });
    expect([...reachableFrom(definition, "DeleteDestinationItem")].sort()).toEqual([
      "DeleteDestinationItem",
      "StatusChangedElsewhere",
    ]);
  });

  it("records its own execution only while DELETING, and reads the item either way", () => {
    expect(states.RecordDeletionExecution.Arguments).toMatchObject({
      ConditionExpression: "#status = :deleting",
    });
    expect(states.RecordDeletionExecution.Next).toBe("ReadDestination");
    expect(
      catchTarget(states.RecordDeletionExecution, "DynamoDb.ConditionalCheckFailedException"),
    ).toBe("ReadDestination");
  });

  it("calls the tenant as the destination role and the table with the workflow's own role", () => {
    for (const [name, state] of taskStates(definition)) {
      const expected = ["s3", "iam", "bedrockagent", "cloudwatchlogs"].includes(
        sdkTarget(state).service,
      )
        ? { RoleArn: executionInput("role_arn") }
        : undefined;
      expect({ name, credentials: state.Credentials }).toEqual({ name, credentials: expected });
    }
  });

  it.each([
    ["bedrockagent", "bedrock"],
    ["cloudwatchlogs", "logs"],
  ])("calls only %s actions the destination role grants", (service, prefix) => {
    const granted = destinationRoleActions().map((action) => action.toLowerCase());
    const called = taskStates(definition)
      .map(([, state]) => sdkTarget(state))
      .filter((target) => target.service === service)
      .map(({ action }) => `${prefix}:${action}`.toLowerCase());
    expect(called.length).toBeGreaterThan(0);
    for (const action of called) expect(granted).toContain(action);
  });

  it("deletes the ingestion log delivery, its source and destination, and its log group before the knowledge base role", () => {
    expect(choiceTargets(states.HasIngestionLogDelivery)).toEqual(["DeleteIngestionLogDelivery"]);
    expect(states.HasIngestionLogDelivery.Default).toBe("DeleteIngestionLogSource");
    expect(
      [
        "DeleteIngestionLogDelivery",
        "DeleteIngestionLogSource",
        "DeleteIngestionLogDestination",
        "DeleteIngestionLogGroup",
      ].map((name) => [
        states[name].Next,
        catchTarget(states[name], "CloudWatchLogs.ResourceNotFoundException"),
      ]),
    ).toEqual([
      ["DeleteIngestionLogSource", "DeleteIngestionLogSource"],
      ["DeleteIngestionLogDestination", "DeleteIngestionLogDestination"],
      ["DeleteIngestionLogGroup", "DeleteIngestionLogGroup"],
      ["DeleteKnowledgeBaseRolePolicy", "DeleteKnowledgeBaseRolePolicy"],
    ]);
  });
});
