import { App, RemovalPolicy, Stack } from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { AttributeType, Table } from "aws-cdk-lib/aws-dynamodb";
import { IGrantable } from "aws-cdk-lib/aws-iam";
import { Key } from "aws-cdk-lib/aws-kms";
import { RetentionDays } from "aws-cdk-lib/aws-logs";
import { Pass } from "aws-cdk-lib/aws-stepfunctions";
import { Construct } from "constructs";
import { ConnectorDeletionWorkflowName } from "../lib/common/config";
import { ConnectorDeletionWorkflow } from "../lib/construct-groups/connector-deletion-workflow";
import { RequestIngestionProps } from "../lib/construct-groups/ingestion-queue";
import { OBJECT_KEYS_PER_PAGE } from "../lib/construct-groups/workflow-states";
import { connectorKeyExample } from "./identifier-examples";
import { evaluate } from "./jsonata-evaluator";
import {
  isCdkLoggingOrTracing,
  partitionJoinArn,
  reachableFrom,
  renderedDefinition,
  roleStatements,
  structuralProblems,
  taskStates,
} from "./state-machine-definition";

const SERVICE_ACCOUNT = "444455556666";
const TENANT_ACCOUNT = "111122223333";
const SCHEDULE_GROUP = "CcfSchedulerGroup";
const RUNTIME_ROLES = `arn:aws:iam::${SERVICE_ACCOUNT}:role/ccf-runtime/CcfConnectorRuntime-*`;
/** The fields of the backend's `ConnectorDeletionWorkflowInput`. */
const DELETION_INPUT_FIELDS = [
  "tenant_arn_prefix",
  "destination_id",
  "connector_id",
  "connector_arn",
  "destination_connector_key",
  "destination_type",
  "bucket_name",
  "destination_role_arn",
  "runtime_role_name",
  "schedule_name",
  "job_definition_arn",
  "correlation_id",
  "api_request_id",
  "continuation",
];

const stack = new Stack(new App(), "Test", {
  env: { account: SERVICE_ACCOUNT, region: "us-east-1" },
});
const table = (id: string, partitionKey: string, sortKey: string): Table =>
  new Table(stack, id, {
    tableName: `Ccf${id}`,
    partitionKey: { name: partitionKey, type: AttributeType.STRING },
    sortKey: { name: sortKey, type: AttributeType.STRING },
  });
const serviceKey = new Key(stack, "ServiceKey");
/** Stands in for `IngestionQueue`, with the two state names its builder makes and its key grant. */
const ingestion = {
  requestIngestion: (scope: Construct, props: RequestIngestionProps) =>
    Pass.jsonata(scope, `${props.statePrefix}SetIngestionPending`).next(
      Pass.jsonata(scope, `${props.statePrefix}SendIngestionMessage`).next(props.next),
    ),
  grantRequestIngestion: (grantee: IGrantable) => {
    serviceKey.grant(grantee, "kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey");
  },
};
new ConnectorDeletionWorkflow(stack, "ConnectorDeletionWorkflow", {
  documentsTable: table("Documents", "connector_arn", "collection_document_key"),
  collectionsTable: table("Collections", "connector_arn", "collection_id"),
  connectorsTable: table("CustomConnectors", "tenant_arn_prefix", "destination_connector_key"),
  ingestion,
  scheduleGroupName: SCHEDULE_GROUP,
  runtimeRoleArnPattern: RUNTIME_ROLES,
  serviceAccountId: SERVICE_ACCOUNT,
  allowedTenantAccountIds: [TENANT_ACCOUNT],
  logRetention: RetentionDays.ONE_MONTH,
  removalPolicy: RemovalPolicy.DESTROY,
});
const template = Template.fromStack(stack);
const definition = renderedDefinition(template, ConnectorDeletionWorkflowName.STATE_MACHINE);
const states = definition.States;
const isS3 = (resource: string | undefined): boolean => (resource ?? "").includes(":aws-sdk:s3:");
const catchTargets = (name: string): string[] =>
  (states[name].Catch ?? []).map((caught) => caught.Next);

/** Every execution input field the definition reads. */
const inputFieldsRead = (): Set<string> =>
  new Set(
    [...JSON.stringify(definition).matchAll(/\$states\.context\.Execution\.Input\.([a-z_]+)/g)].map(
      ([, field]) => field,
    ),
  );

describe("CcfConnectorDeletionWorkflow", () => {
  test("has unique state names, no missing targets, and no unreachable states", () => {
    expect(structuralProblems(definition)).toEqual([]);
  });

  test("the schedule, then files, then records, then the runtime role, the ingestion, and the item", () => {
    expect(definition.StartAt).toBe("StartPassCount");
    expect(states.StartPassCount.Next).toBe("DeleteSchedule");
    expect(states.DeleteSchedule.Next).toBe("ListConnectorObjects");
    expect(states.HasConnectorObjects.Choices?.[0].Next).toBe("QueryDocuments");
    expect(states.HasDocuments.Default).toBe("QueryCollections");
    expect(states.HasCollections.Default).toBe("DeleteRuntimeRolePolicy");
    expect(states.DeleteRuntimeRole.Next).toBe("HasJobDefinition");
    expect(states.HasJobDefinition.Default).toBe("DeletionSetIngestionPending");
    expect(states.DeregisterJobDefinition.Next).toBe("DeletionSetIngestionPending");
    expect(reachableFrom(definition, "DeleteRuntimeRole")).toContain("DeregisterJobDefinition");
    expect(reachableFrom(definition, "DeregisterJobDefinition")).not.toContain("DeleteRuntimeRole");
    expect(states.DeletionSendIngestionMessage.Next).toBe("DeleteConnectorItem");
    expect(states.DeleteConnectorItem.End).toBe(true);
    expect(reachableFrom(definition, "QueryDocuments")).not.toContain("DeleteConnectorObjects");
    expect(reachableFrom(definition, "DeleteRuntimeRole")).not.toContain("DeleteDocumentsRecords");
    expect([...reachableFrom(definition, "DeleteConnectorItem")]).toEqual(["DeleteConnectorItem"]);
  });

  test("every S3 call runs as the destination's role, and no other call does", () => {
    const tasks = taskStates(definition);
    expect(
      tasks
        .filter(([, task]) => isS3(task.Resource))
        .map(([name]) => name)
        .sort(),
    ).toEqual(["DeleteConnectorObjects", "ListConnectorObjects"]);
    for (const [, task] of tasks)
      expect(task.Credentials).toEqual(
        isS3(task.Resource)
          ? { RoleArn: "{% $states.context.Execution.Input.destination_role_arn %}" }
          : undefined,
      );
  });

  test("the object loop lists the example custom connector's key prefix, a page that fits the payload limit", async () => {
    const { connectorId, keyStart, keyEnd } = connectorKeyExample();
    const listing = states.ListConnectorObjects.Arguments ?? {};
    expect({ ...listing, Prefix: undefined }).toEqual({
      Bucket: "{% $states.context.Execution.Input.bucket_name %}",
      MaxKeys: OBJECT_KEYS_PER_PAGE,
      FetchOwner: false,
      Prefix: undefined,
    });
    const prefix = await evaluate({
      expression: String(listing.Prefix),
      bindings: { states: { context: { Execution: { Input: { connector_id: connectorId } } } } },
    });
    expect(prefix).toBe(`${keyStart}${connectorId}${keyEnd}`);
  });

  test("each query reads its table's keys for the custom connector, 25 at a time, and keys the batch delete by the table's name", () => {
    for (const [name, tableName, sortKey] of [
      ["Documents", "CcfDocuments", "collection_document_key"],
      ["Collections", "CcfCollections", "collection_id"],
    ]) {
      expect(states[`Query${name}`].Arguments).toEqual({
        TableName: tableName,
        KeyConditionExpression: "connector_arn = :connector_arn",
        ExpressionAttributeValues: {
          ":connector_arn": { S: "{% $states.context.Execution.Input.connector_arn %}" },
        },
        ProjectionExpression: `connector_arn, ${sortKey}`,
        Limit: 25,
      });
      expect(states[`Delete${name}Records`].Arguments).toEqual({
        RequestItems: { [tableName]: "{% $states.input.DeleteRequests %}" },
      });
    }
  });

  test("no Map state iterates over a list or query response", () => {
    expect(Object.values(states).filter((state) => state.Type === "Map")).toEqual([]);
  });

  test("each not-found catch continues to the next cleanup step", () => {
    expect(catchTargets("DeleteSchedule")).toEqual(["ListConnectorObjects"]);
    expect(catchTargets("ListConnectorObjects")).toEqual(["QueryDocuments"]);
    expect(catchTargets("DeleteRuntimeRolePolicy")).toEqual(["DeleteRuntimeRole"]);
    expect(catchTargets("DeleteRuntimeRole")).toEqual(["HasJobDefinition"]);
    expect(catchTargets("DeregisterJobDefinition")).toEqual(["DeletionSetIngestionPending"]);
    expect(catchTargets("RecordContinuedDeletion")).toEqual(["DeletionContinued"]);
  });

  test("the workflow catches only not-found errors", () => {
    const notFound = new Set([
      "Scheduler.ResourceNotFoundException",
      "S3.NoSuchBucketException",
      "Iam.NoSuchEntityException",
      "DynamoDb.ConditionalCheckFailedException",
      "Batch.ClientException",
    ]);
    for (const state of Object.values(states))
      for (const caught of state.Catch ?? [])
        for (const error of caught.ErrorEquals) expect(notFound).toContain(error);
  });

  test("the runtime role policy and the schedule group are the backend's names", () => {
    expect(states.DeleteRuntimeRolePolicy.Arguments?.PolicyName).toBe("CcfConnectorRuntimePolicy");
    expect(states.DeleteSchedule.Arguments?.GroupName).toBe(SCHEDULE_GROUP);
  });

  test("the ASL reads only ConnectorDeletionWorkflowInput's fields", () => {
    for (const field of inputFieldsRead()) expect(DELETION_INPUT_FIELDS).toContain(field);
  });

  test("every loop continues into a numbered execution after its passes, and records it", () => {
    for (const name of ["HasConnectorObjects", "HasDocuments", "HasCollections"])
      expect(
        (states[name].Choices ?? []).some((choice) => choice.Next === "CheckContinuations"),
      ).toBe(true);
    const name =
      "$states.context.Execution.Input.connector_id & '-delete-' & " +
      "$substring($states.context.Execution.Input.api_request_id, 0, 8) & " +
      "'-' & $string($states.context.Execution.Input.continuation + 1)";
    expect(states.ContinueDeletion.Arguments?.Name).toBe(`{% ${name} %}`);
    expect(states.ContinueDeletion.Next).toBe("RecordContinuedDeletion");
    expect(states.RecordContinuedDeletion.Arguments).toMatchObject({
      UpdateExpression: "SET deletion_execution_arn = :arn",
      ConditionExpression: "attribute_exists(destination_connector_key)",
    });
    expect(states.CheckContinuations.Default).toBe("ContinueDeletion");
  });

  test("CcfConnectorDeletionWorkflowRole holds exactly these statements, none on s3: or sts:AssumeRole *", () => {
    const tableArn = (name: string): unknown => {
      const [id] = Object.keys(
        template.findResources("AWS::DynamoDB::Table", { Properties: { TableName: name } }),
      );
      return { "Fn::GetAtt": [id, "Arn"] };
    };
    const [keyId] = Object.keys(template.findResources("AWS::KMS::Key"));
    const statements = roleStatements(template, ConnectorDeletionWorkflowName.ROLE).filter(
      (statement) => !isCdkLoggingOrTracing(statement),
    );
    /** Each statement once, in one order: each S3 task adds its own copy of the AssumeRole statement. */
    const distinct = (list: readonly unknown[]): string[] =>
      [...new Set(list.map((statement) => JSON.stringify(statement)))].sort();
    const regionalArn = (service: string, rest: string): unknown => ({
      "Fn::Join": [
        "",
        [
          "arn:",
          { Ref: "AWS::Partition" },
          `:${service}:`,
          { Ref: "AWS::Region" },
          ":",
          { Ref: "AWS::AccountId" },
          rest,
        ],
      ],
    });
    expect(distinct(statements)).toEqual(
      distinct([
        {
          Effect: "Allow",
          Action: ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey"],
          Resource: [{ "Fn::GetAtt": [keyId, "Arn"] }],
        },
        {
          Effect: "Allow",
          Action: ["dynamodb:deleteItem"],
          Resource: [tableArn("CcfCustomConnectors")],
        },
        {
          Effect: "Allow",
          Action: ["iam:deleteRole"],
          Resource: [RUNTIME_ROLES],
        },
        {
          Effect: "Allow",
          Action: ["iam:deleteRolePolicy"],
          Resource: [RUNTIME_ROLES],
        },
        {
          Effect: "Allow",
          Action: ["dynamodb:batchWriteItem"],
          Resource: [tableArn("CcfCollections")],
        },
        { Effect: "Allow", Action: ["dynamodb:query"], Resource: [tableArn("CcfCollections")] },
        {
          Effect: "Allow",
          Action: ["dynamodb:batchWriteItem"],
          Resource: [tableArn("CcfDocuments")],
        },
        { Effect: "Allow", Action: ["dynamodb:query"], Resource: [tableArn("CcfDocuments")] },
        {
          Effect: "Allow",
          Action: ["states:startExecution"],
          Resource: [regionalArn("states", ":stateMachine:CcfConnectorDeletionWorkflow")],
        },
        {
          // The tenant task role's own statement, for the first destination role pattern.
          Effect: "Allow",
          Action: ["sts:AssumeRole"],
          Resource: [
            partitionJoinArn(`:iam::${SERVICE_ACCOUNT}:role/ccf/destinations/CcfDestinationRole-*`),
          ],
        },
        {
          Effect: "Allow",
          Action: ["dynamodb:updateItem"],
          Resource: [tableArn("CcfCustomConnectors")],
        },
        {
          Effect: "Allow",
          Action: ["sts:AssumeRole"],
          Resource: [
            partitionJoinArn(`:iam::${SERVICE_ACCOUNT}:role/ccf/destinations/CcfDestinationRole-*`),
            partitionJoinArn(`:iam::${TENANT_ACCOUNT}:role/ccf/destinations/CcfDestinationRole-*`),
          ],
        },
        {
          Effect: "Allow",
          Action: ["batch:deregisterJobDefinition"],
          Resource: [regionalArn("batch", ":job-definition/CcfJob-*:*")],
        },
        {
          Effect: "Allow",
          Action: ["scheduler:deleteSchedule"],
          Resource: [regionalArn("scheduler", `:schedule/${SCHEDULE_GROUP}/*`)],
        },
      ]),
    );
  });

  test("the role is CcfConnectorDeletionWorkflowRole under /ccf/ and the machine is Standard, traced, and logged", () => {
    template.hasResourceProperties("AWS::IAM::Role", {
      RoleName: "CcfConnectorDeletionWorkflowRole",
      Path: "/ccf/",
    });
    template.hasResourceProperties("AWS::StepFunctions::StateMachine", {
      StateMachineName: "CcfConnectorDeletionWorkflow",
      StateMachineType: "STANDARD",
      TracingConfiguration: { Enabled: true },
      LoggingConfiguration: { Level: "ERROR", IncludeExecutionData: true },
    });
  });
});
