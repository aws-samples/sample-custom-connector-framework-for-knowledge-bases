import { App, RemovalPolicy, Stack } from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { AttributeType, Table } from "aws-cdk-lib/aws-dynamodb";
import { Role, ServicePrincipal } from "aws-cdk-lib/aws-iam";
import { RetentionDays } from "aws-cdk-lib/aws-logs";
import { StateMachine } from "aws-cdk-lib/aws-stepfunctions";
import { SERVICE_SETTINGS } from "../lib/common/config";
import { destinationBucketPolicy } from "../lib/common/destination-bucket-policy";
import { DestinationWorkflowName } from "../lib/common/destination-names";
import {
  CLEANUP_ERROR,
  QUOTA_ERRORS,
  destinationCreationStates,
  failureReason,
} from "../lib/construct-groups/destination-creation-states";
import { executionInput, jsonataStateMachine } from "../lib/construct-groups/workflow-states";
import { evaluate } from "./jsonata-evaluator";
import {
  AslTestDefinition,
  AslTestState,
  reachableFrom,
  renderedDefinition,
  structuralProblems,
  taskStates,
} from "./state-machine-definition";

const TENANT_ACCOUNT = "111122223333";
const DESTINATION_ID = "dst-7c41e9a2b5d0";
const SETTINGS = { ...SERVICE_SETTINGS.destinationWorkflow, maxPolls: 7 };
const INPUT = {
  destination_id: DESTINATION_ID,
  tenant_arn_prefix: `arn:aws:ccf:us-west-2:${TENANT_ACCOUNT}`,
  role_arn: `arn:aws:iam::${TENANT_ACCOUNT}:role/ccf/destinations/CcfDestinationRole-docs`,
  type: "BEDROCK",
  access_control_enabled: true,
  correlation_id: "correlation-1",
  api_request_id: "request-1",
  execution_name: `${DESTINATION_ID}-create-1762000000000`,
  continuation: 0,
  action: "CREATE",
  boundary_arn: `arn:aws:iam::${TENANT_ACCOUNT}:policy/ccf/destinations/CcfDestinationBoundary-docs`,
};
const CONDITION_FAILED = "DynamoDb.ConditionalCheckFailedException";

const synthesize = (region: string): AslTestDefinition => {
  const stack = new Stack(new App(), "Workflow", { env: { account: "444455556666", region } });
  const destinationsTable = new Table(stack, "Destinations", {
    partitionKey: { name: "tenant_arn_prefix", type: AttributeType.STRING },
    sortKey: { name: "destination_id", type: AttributeType.STRING },
  });
  jsonataStateMachine(stack, "Machine", {
    stateMachineName: DestinationWorkflowName.STATE_MACHINE,
    definition: destinationCreationStates(stack, {
      destinationsTable,
      destinationRoleArns: [
        `arn:aws:iam::${TENANT_ACCOUNT}:role/ccf/destinations/CcfDestinationRole-*`,
      ],
      deletionStateMachine: StateMachine.fromStateMachineName(
        stack,
        "Deletion",
        DestinationWorkflowName.DELETION_STATE_MACHINE,
      ),
      region,
      deletionProtectionThreshold: 15,
      ingestionLogRetention: RetentionDays.ONE_MONTH,
      settings: SETTINGS,
    }),
    role: new Role(stack, "Role", { assumedBy: new ServicePrincipal("states.amazonaws.com") }),
    logRetention: RetentionDays.ONE_WEEK,
    removalPolicy: RemovalPolicy.DESTROY,
  });
  return renderedDefinition(Template.fromStack(stack), DestinationWorkflowName.STATE_MACHINE);
};

/** A value with every `{% %}` leaf evaluated against the bindings. */
const render = async (value: unknown, bindings: Record<string, unknown>): Promise<unknown> => {
  if (typeof value === "string" && value.startsWith("{%"))
    return evaluate({ expression: value, bindings });
  if (Array.isArray(value)) return Promise.all(value.map((item) => render(item, bindings)));
  if (typeof value === "object" && value !== null)
    return Object.fromEntries(
      await Promise.all(
        Object.entries(value).map(async ([key, item]) => [key, await render(item, bindings)]),
      ),
    );
  return value;
};

const catchTarget = (state: AslTestState, error: string): string | undefined =>
  state.Catch?.find((caught) => caught.ErrorEquals.includes(error))?.Next;

const choiceTargets = (state: AslTestState): string[] =>
  (state.Choices ?? []).map((choice) => choice.Next);

const isTenantCall = (state: AslTestState): boolean =>
  /aws-sdk:(s3|iam|bedrockagent|cloudwatchlogs):/.test(String(state.Resource));

describe("destinationCreationStates", () => {
  const definition = synthesize("us-west-2");
  const { States: states } = definition;
  const context = { Execution: { Input: INPUT, Id: "arn:execution" } };
  /** The variables `ResolveNames` and `RenderDocuments` assign, evaluated for INPUT. */
  const names = async (): Promise<Record<string, unknown>> => {
    const resolved = await render(states.ResolveNames.Assign, { states: { context } });
    const bindings = { ...Object(resolved), states: { context } };
    const rendered = await render(states.RenderDocuments.Assign, bindings);
    return { ...bindings, ...Object(rendered) };
  };

  it("has no repeated or out-of-scope state name", () => {
    expect(structuralProblems(definition)).toEqual([]);
  });

  it("polls the knowledge base it created: wait, get, check", () => {
    expect(states.WaitForKnowledgeBase.Next).toBe("GetKnowledgeBase");
    expect(states.GetKnowledgeBase.Next).toBe("CheckKnowledgeBase");
  });

  it("has no JSONPath state", () => {
    for (const state of Object.values(states)) {
      expect(state.Parameters).toBeUndefined();
      expect(state.ResultPath).toBeUndefined();
    }
    expect(JSON.stringify(definition)).not.toContain("States.Format");
  });

  it("records its own execution first, only on an existing item", () => {
    expect(states.ResolveNames.Next).toBe("RecordExecution");
    expect(states.RecordExecution.Arguments).toMatchObject({
      UpdateExpression: "SET workflow_execution_arn = :arn",
      ConditionExpression: "attribute_exists(tenant_arn_prefix)",
      ExpressionAttributeValues: { ":arn": { S: "{% $states.context.Execution.Id %}" } },
    });
    expect(catchTarget(states.RecordExecution, CONDITION_FAILED)).toBe("StatusChangedElsewhere");
  });

  it("sends DELETE to the deletion child and CREATE to the bucket", () => {
    expect(choiceTargets(states.ChooseAction)).toEqual(["DeleteDestination", "RenderDocuments"]);
    expect(states.ChooseAction.Default).toBeUndefined();
    expect(states.RenderDocuments.Next).toBe("CreateBucket");
  });

  it("runs the deletion child with .sync and this execution's input, failing with its error", () => {
    const child = states.DeleteDestination;
    expect(child.Resource).toContain("states:startExecution.sync");
    expect(child.Arguments?.Name).toBe(executionInput("execution_name"));
    expect(child.Arguments?.Input).toMatchObject({
      action: executionInput("action"),
      role_arn: executionInput("role_arn"),
      execution_name: executionInput("execution_name"),
    });
    expect(catchTarget(child, "States.ALL")).toBe("DeletionFailed");
    expect(states.DeletionFailed).toMatchObject({
      Type: "Fail",
      Error: "{% $states.input.Error %}",
    });
  });

  it("sends every other error of a create call to RecordFailure with the error in $error", () => {
    const creates = taskStates(definition).filter(([, state]) => isTenantCall(state));
    expect(creates.length).toBe(16);
    for (const [, state] of creates) {
      const caught = state.Catch?.at(-1);
      expect(caught).toMatchObject({
        ErrorEquals: ["States.ALL"],
        Next: "RecordFailure",
        Assign: { error: "{% $states.errorOutput %}" },
      });
    }
  });

  it("sends a quota error to CleanUpAfterQuotaError and then MarkFailed, and every other error to MarkFailed", async () => {
    const [quota] = states.RecordFailure.Choices ?? [];
    expect(quota.Next).toBe("CleanUpAfterQuotaError");
    expect(states.RecordFailure.Default).toBe("MarkFailed");
    for (const error of QUOTA_ERRORS)
      expect(
        await evaluate({
          expression: String(quota.Condition),
          bindings: { error: { Error: error } },
        }),
      ).toBe(true);
    expect(
      await evaluate({
        expression: String(quota.Condition),
        bindings: { error: { Error: "S3.AccessDenied" } },
      }),
    ).toBe(false);
    expect(states.CleanUpAfterQuotaError.Next).toBe("MarkFailed");
    expect(catchTarget(states.CleanUpAfterQuotaError, "States.ALL")).toBe("MarkFailed");
  });

  it("runs the quota cleanup as a DELETE of the same destination, bounded by the configured timeout", () => {
    const cleanUp = states.CleanUpAfterQuotaError;
    expect(cleanUp.Resource).toContain("states:startExecution.sync");
    expect(cleanUp.TimeoutSeconds).toBe(SETTINGS.quotaCleanupTimeout.toSeconds());
    expect(cleanUp.Retry).toBeUndefined();
    expect(cleanUp.Arguments?.Input).toMatchObject({ action: "DELETE", continuation: 0 });
    expect(String(cleanUp.Arguments?.Name)).toContain("'-cleanup'");
  });

  it("ends every path after a create failure in CreationFailed or StatusChangedElsewhere", () => {
    const ends = [...reachableFrom(definition, "RecordFailure")].filter((name) =>
      ["Fail", "Succeed"].includes(states[name].Type),
    );
    expect(ends.sort()).toEqual(["CreationFailed", "StatusChangedElsewhere"]);
  });

  it("builds the KnowledgeBaseTimedOut cause from the configured count and interval", () => {
    const [, failed, timedOut] = states.CheckKnowledgeBase.Choices ?? [];
    expect(failed).toMatchObject({
      Next: "RecordFailure",
      Assign: { error: { Error: "KnowledgeBaseFailed" } },
    });
    expect(String(timedOut.Condition)).toContain(">= 7");
    expect(timedOut.Assign).toEqual({
      error: {
        Error: "KnowledgeBaseTimedOut",
        Cause: "The knowledge base wasn't ACTIVE after 7 checks 30 seconds apart",
      },
    });
  });

  it("truncates a long status_reason to 2,048 characters, keeping the correlation ID", async () => {
    const reason = await evaluate({
      expression: failureReason(),
      bindings: {
        error: { Error: "S3.InternalError", Cause: "x".repeat(3_000) },
        cleanupError: null,
        states: { context },
      },
    });
    expect(String(reason)).toHaveLength(2_048);
    expect(String(reason).startsWith("S3.InternalError: xxx")).toBe(true);
    expect(String(reason).endsWith("x (correlation correlation-1)")).toBe(true);
    expect(String(reason)).toContain(" ... ");
  });

  it("names an incomplete cleanup in the reason", async () => {
    const reason = await evaluate({
      expression: failureReason(),
      bindings: {
        error: { Error: "S3.TooManyBucketsException", Cause: "too many" },
        cleanupError: "BucketObjectsNotDeleted",
        states: { context },
      },
    });
    expect(reason).toBe(
      "S3.TooManyBucketsException: too many; cleanup incomplete: BucketObjectsNotDeleted (correlation correlation-1)",
    );
    expect(states.MarkFailed.Arguments).toMatchObject({
      ExpressionAttributeValues: { ":reason": { S: failureReason() } },
    });
  });

  it.each([
    [
      {
        Error: "States.TaskFailed",
        Cause: JSON.stringify({ Status: "FAILED", Error: "DataSourceDeletionFailed" }),
      },
      "DataSourceDeletionFailed",
    ],
    [
      {
        Error: "States.TaskFailed",
        Cause: JSON.stringify({ status: "FAILED", error: "DeletionNotFinished" }),
      },
      "DeletionNotFinished",
    ],
    [{ Error: "States.Timeout", Cause: "timed out" }, "States.Timeout"],
  ])("reads the cleanup's error from %j", async (errorOutput, expected) => {
    expect(
      await evaluate({ expression: CLEANUP_ERROR, bindings: { states: { errorOutput } } }),
    ).toBe(expected);
  });

  it("sets FAILED only from a CREATING_* status, and publishes DestinationCreationFailed with no dimension", () => {
    expect(states.MarkFailed.Arguments).toMatchObject({
      ConditionExpression: "#status IN (:creating0, :creating1, :creating2)",
      ExpressionAttributeValues: {
        ":creating0": { S: "CREATING_BUCKET" },
        ":creating1": { S: "CREATING_KNOWLEDGE_BASE" },
        ":creating2": { S: "CREATING_DATA_SOURCE" },
        ":failed": { S: "FAILED" },
      },
    });
    expect(catchTarget(states.MarkFailed, CONDITION_FAILED)).toBe("StatusChangedElsewhere");
    expect(states.PublishCreationFailed.Arguments).toEqual({
      Namespace: "CustomConnectorFramework",
      MetricData: [{ MetricName: "DestinationCreationFailed", Value: 1, Unit: "Count" }],
    });
    expect(states.PublishCreationFailed.Next).toBe("CreationFailed");
  });

  it("calls the tenant as the destination role and the table and metrics with the workflow's own role", () => {
    for (const [name, state] of taskStates(definition)) {
      const expected = isTenantCall(state) ? { RoleArn: executionInput("role_arn") } : undefined;
      expect({ name, credentials: state.Credentials }).toEqual({ name, credentials: expected });
    }
  });

  it("passes the Region as LocationConstraint outside us-east-1 and nothing in us-east-1", () => {
    expect(states.CreateBucket.Arguments?.CreateBucketConfiguration).toEqual({
      LocationConstraint: "us-west-2",
    });
    expect(
      synthesize("us-east-1").States.CreateBucket.Arguments?.CreateBucketConfiguration,
    ).toBeUndefined();
  });

  it.each([
    ["SetCreatingKnowledgeBase", "CREATING_BUCKET", "CREATING_KNOWLEDGE_BASE"],
    ["SetActiveWithoutKnowledgeBase", "CREATING_BUCKET", "ACTIVE"],
    ["RecordKnowledgeBase", "CREATING_KNOWLEDGE_BASE", undefined],
    ["SetCreatingDataSource", "CREATING_KNOWLEDGE_BASE", "CREATING_DATA_SOURCE"],
    ["SetActive", "CREATING_DATA_SOURCE", "ACTIVE"],
  ])("moves %s only from %s", (name, from, to) => {
    const values = states[name].Arguments?.ExpressionAttributeValues;
    expect(states[name].Arguments?.ConditionExpression).toBe("#status = :expected");
    expect(values).toMatchObject({ ":expected": { S: from } });
    expect(values).toMatchObject(to === undefined ? {} : { ":next": { S: to } });
    expect(catchTarget(states[name], CONDITION_FAILED)).toBe("StatusChangedElsewhere");
  });

  it("records the knowledge base ID, which the deletion reads", () => {
    expect(states.RecordKnowledgeBase.Arguments?.ExpressionAttributeValues).toMatchObject({
      ":resources": { M: { knowledge_base_id: { S: "{% $knowledgeBaseId %}" } } },
    });
  });

  it("chooses the knowledge base path for BEDROCK only", () => {
    expect(choiceTargets(states.ChooseDestinationType)).toEqual(["SetCreatingKnowledgeBase"]);
    expect(String(states.ChooseDestinationType.Choices?.[0].Condition)).toContain("'BEDROCK'");
    expect(states.ChooseDestinationType.Default).toBe("SetActiveWithoutKnowledgeBase");
  });

  it("blocks public access, encrypts with AES256, and tags the bucket with the destination ID", () => {
    expect(states.BlockPublicAccess.Arguments?.PublicAccessBlockConfiguration).toEqual({
      BlockPublicAcls: true,
      IgnorePublicAcls: true,
      BlockPublicPolicy: true,
      RestrictPublicBuckets: true,
    });
    expect(states.EncryptBucket.Arguments?.ServerSideEncryptionConfiguration).toEqual({
      Rules: [{ ApplyServerSideEncryptionByDefault: { SseAlgorithm: "AES256" } }],
    });
    expect(states.TagBucket.Arguments?.Tagging).toEqual({
      TagSet: [{ Key: "ccf:destination-id", Value: executionInput("destination_id") }],
    });
  });

  it("treats its own bucket and role as created on a rerun and sends fixed client tokens", () => {
    expect(catchTarget(states.CreateBucket, "S3.BucketAlreadyOwnedByYouException")).toBe(
      "BlockPublicAccess",
    );
    expect(catchTarget(states.CreateKnowledgeBaseRole, "Iam.EntityAlreadyExistsException")).toBe(
      "PutKnowledgeBaseRolePolicy",
    );
    expect(states.CreateKnowledgeBase.Arguments?.ClientToken).toBe("{% $knowledgeBaseToken %}");
    expect(states.CreateDataSource.Arguments?.ClientToken).toBe("{% $dataSourceToken %}");
    expect(states.ResolveNames.Assign).toMatchObject({
      knowledgeBaseToken: `{% $hash('knowledge-base-' & $states.context.Execution.Input.destination_id, 'SHA-256') %}`,
      dataSourceToken: `{% $hash('data-source-' & $states.context.Execution.Input.destination_id, 'SHA-256') %}`,
    });
  });

  it("names the bucket ccf-<destination ID>-<tenant account>-<region>", async () => {
    expect((await names()).bucketName).toBe(
      `ccf-${DESTINATION_ID}-${TENANT_ACCOUNT}-\${AWS::Region}`,
    );
  });

  it("puts the bucket policy: one DenyInsecureTransport statement and no Allow", async () => {
    const bucketArn = `arn:\${AWS::Partition}:s3:::ccf-${DESTINATION_ID}-${TENANT_ACCOUNT}-\${AWS::Region}`;
    expect((await names()).bucketPolicy).toEqual(
      destinationBucketPolicy({ bucketArn, objectsArn: `${bucketArn}/*` }),
    );
    expect(states.PutBucketPolicy.Arguments?.Policy).toBe("{% $string($bucketPolicy) %}");
  });

  it("lets only Bedrock knowledge bases in the tenant account assume the knowledge base role", async () => {
    expect((await names()).trustPolicy).toEqual({
      Version: "2012-10-17",
      Statement: [
        {
          Effect: "Allow",
          Principal: { Service: "bedrock.amazonaws.com" },
          Action: "sts:AssumeRole",
          Condition: {
            StringEquals: { "aws:SourceAccount": TENANT_ACCOUNT },
            ArnLike: {
              "aws:SourceArn": `arn:\${AWS::Partition}:bedrock:\${AWS::Region}:${TENANT_ACCOUNT}:knowledge-base/*`,
            },
          },
        },
      ],
    });
  });

  it("creates the knowledge base role tagged, under /ccf/destinations/, with the destination's boundary", async () => {
    expect(states.CreateKnowledgeBaseRole.Arguments).toMatchObject({
      RoleName: "{% $knowledgeBaseRoleName %}",
      Path: "/ccf/destinations/",
      AssumeRolePolicyDocument: "{% $string($trustPolicy) %}",
      PermissionsBoundary: executionInput("boundary_arn"),
      Tags: [{ Key: "ccf:destination-id", Value: executionInput("destination_id") }],
    });
    expect((await names()).knowledgeBaseRoleArn).toBe(
      `arn:\${AWS::Partition}:iam::${TENANT_ACCOUNT}:role/ccf/destinations/CcfKnowledgeBase-${DESTINATION_ID}`,
    );
  });

  it("lets the knowledge base role read only its own bucket and objects, and publish only Bedrock's metrics", async () => {
    const bucketArn = `arn:\${AWS::Partition}:s3:::ccf-${DESTINATION_ID}-${TENANT_ACCOUNT}-\${AWS::Region}`;
    expect((await names()).readPolicy).toEqual({
      Version: "2012-10-17",
      Statement: [
        {
          Effect: "Allow",
          Action: ["s3:GetObject*", "s3:GetBucket*", "s3:List*"],
          Resource: [bucketArn, `${bucketArn}/*`],
        },
        {
          Effect: "Allow",
          Action: "cloudwatch:PutMetricData",
          Resource: "*",
          Condition: { StringEquals: { "cloudwatch:namespace": "AWS/Bedrock/KnowledgeBases" } },
        },
      ],
    });
  });

  it("delivers the knowledge base's ingestion logs to its own log group, then sets ACTIVE", async () => {
    const bindings = await names();
    expect(states.CreateDataSource.Next).toBe("CreateIngestionLogGroup");
    expect(
      [
        "CreateIngestionLogGroup",
        "PutIngestionLogRetention",
        "PutIngestionLogSource",
        "PutIngestionLogDestination",
        "CreateIngestionLogDelivery",
      ].map((name) => states[name].Next),
    ).toEqual([
      "PutIngestionLogRetention",
      "PutIngestionLogSource",
      "PutIngestionLogDestination",
      "CreateIngestionLogDelivery",
      "SetActive",
    ]);
    expect([bindings.ingestionLogGroupName, bindings.ingestionLogGroupArn]).toEqual([
      `/aws/vendedlogs/bedrock/knowledge-base/ccf-${DESTINATION_ID}`,
      `arn:\${AWS::Partition}:logs:\${AWS::Region}:${TENANT_ACCOUNT}:log-group:/aws/vendedlogs/bedrock/knowledge-base/ccf-${DESTINATION_ID}`,
    ]);
    expect(states.PutIngestionLogRetention.Arguments).toMatchObject({ RetentionInDays: 30 });
    expect(states.PutIngestionLogSource.Arguments).toMatchObject({ LogType: "APPLICATION_LOGS" });
  });

  it("treats its own log group and delivery as created on a rerun", () => {
    expect(
      catchTarget(states.CreateIngestionLogGroup, "CloudWatchLogs.ResourceAlreadyExistsException"),
    ).toBe("PutIngestionLogRetention");
    expect(catchTarget(states.CreateIngestionLogDelivery, "CloudWatchLogs.ConflictException")).toBe(
      "SetActive",
    );
  });

  it("writes the empty global ACL file only with access control on", () => {
    expect(choiceTargets(states.ChooseAccessControl)).toEqual(["WriteGlobalAclFile"]);
    expect(states.ChooseAccessControl.Default).toBe("CreateDataSource");
    expect(states.WriteGlobalAclFile.Arguments).toMatchObject({
      Key: "ccf-acl/global-acl.json",
      Body: "[]",
    });
    expect(states.WriteGlobalAclFile.Next).toBe("CreateDataSource");
  });

  it.each([true, false])(
    "sends the S3 connector parameters with access control %s",
    async (enabled) => {
      const bindings = await names();
      const parameters = await evaluate({
        expression: String(
          Object(
            Object(states.CreateDataSource.Arguments?.DataSourceConfiguration)
              .ManagedKnowledgeBaseConnectorConfiguration,
          ).ConnectorParameters,
        ),
        bindings: {
          ...bindings,
          states: {
            context: { Execution: { Input: { ...INPUT, access_control_enabled: enabled } } },
          },
        },
      });
      const bucketName = `ccf-${DESTINATION_ID}-${TENANT_ACCOUNT}-\${AWS::Region}`;
      expect(parameters).toEqual({
        type: "S3",
        version: "1",
        connectionConfiguration: { bucketName, bucketOwnerAccountId: TENANT_ACCOUNT },
        filterConfiguration: { exclusionPrefixes: ["ccf-acl/"] },
        aclEnabled: enabled,
        ...(enabled
          ? {
              aclConfiguration: {
                globalAccessControlListS3Uri: `s3://${bucketName}/ccf-acl/global-acl.json`,
              },
            }
          : {}),
      });
    },
  );

  it("creates a MANAGED knowledge base and a managed S3 data source with deletion protection", () => {
    expect(states.CreateKnowledgeBase.Arguments).toMatchObject({
      KnowledgeBaseConfiguration: {
        Type: "MANAGED",
        ManagedKnowledgeBaseConfiguration: { EmbeddingModelType: "MANAGED" },
      },
      Tags: { "ccf:destination-id": executionInput("destination_id") },
    });
    expect(states.CreateDataSource.Arguments).toMatchObject({
      DataDeletionPolicy: "DELETE",
      DataSourceConfiguration: {
        Type: "MANAGED_KNOWLEDGE_BASE_CONNECTOR",
        ManagedKnowledgeBaseConnectorConfiguration: {
          DeletionProtectionConfiguration: {
            DeletionProtectionStatus: "ENABLED",
            DeletionProtectionThreshold: 15,
          },
        },
      },
    });
  });
});
