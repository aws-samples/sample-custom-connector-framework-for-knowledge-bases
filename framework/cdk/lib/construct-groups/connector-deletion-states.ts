import { Aws } from "aws-cdk-lib";
import { ITable } from "aws-cdk-lib/aws-dynamodb";
import { Choice, Condition, Fail, IChainable, Succeed } from "aws-cdk-lib/aws-stepfunctions";
import { Construct } from "constructs";
import { ConnectorDeletionWorkflowName, JobExecutionName } from "../common/config";
import { tablesContract } from "../common/contracts";
import { RequestIngestionProps } from "./ingestion-queue";
import { RuntimeRoleName } from "./runtime-identities";
import {
  ADD_PASS,
  MAX_CONTINUATIONS,
  SdkService,
  TenantCredentials,
  continueAsNew,
  continuationsSpent,
  executionInput,
  objectDeletionLoop,
  passBudgetSpent,
  sdkCall,
  sdkError,
  startPassCount,
} from "./workflow-states";

/** The ingestion request builder `IngestionQueue` offers; the deletion asks for one more ingestion. */
export interface ConnectorDeletionIngestion {
  requestIngestion(scope: Construct, props: RequestIngestionProps): IChainable;
}

export interface ConnectorDeletionStatesProps {
  /** `CcfDocuments`, keyed by `connector_arn` and `collection_document_key`. */
  readonly documentsTable: ITable;
  /** `CcfCollections`, keyed by `connector_arn` and `collection_id`. */
  readonly collectionsTable: ITable;
  /** `CcfCustomConnectors`, whose item the workflow deletes last. */
  readonly connectorsTable: ITable;
  readonly ingestion: ConnectorDeletionIngestion;
  /** The Scheduler group that holds every custom connector's schedule. */
  readonly scheduleGroupName: string;
  /** The destination roles the workflow role may assume: `destinationRoleArns(...)`. */
  readonly destinationRoleArns: readonly string[];
  /** Every custom connector runtime role: `RuntimeIdentities.runtimeRoleArnPattern`. */
  readonly runtimeRoleArnPattern: string;
}

/** At most 25 deletes per `BatchWriteItem`. */
const BATCH_WRITE_LIMIT = 25;
const NOT_FOUND = {
  schedule: sdkError({ service: SdkService.SCHEDULER, errorName: "ResourceNotFoundException" }),
  role: sdkError({ service: SdkService.IAM, errorName: "NoSuchEntityException" }),
  item: sdkError({ service: SdkService.DYNAMODB, errorName: "ConditionalCheckFailedException" }),
  definition: sdkError({ service: SdkService.BATCH, errorName: "ClientException" }),
};
/** The value of an execution input field inside a JSONata expression. */
const input = (field: string): string => `$states.context.Execution.Input.${field}`;
const STATE_MACHINE_ARN = `arn:${Aws.PARTITION}:states:${Aws.REGION}:${Aws.ACCOUNT_ID}:stateMachine:${ConnectorDeletionWorkflowName.STATE_MACHINE}`;
/** `<connector_id>-delete-<first 8 of api_request_id>-<continuation + 1>`. */
const CONTINUED_EXECUTION_NAME =
  `${input("connector_id")} & '-delete-' & $substring(${input("api_request_id")}, 0, 8) & ` +
  `'-' & $string(${input("continuation")} + 1)`;

/** The custom connector item's key, from the execution input. */
const connectorKey = (): Record<string, unknown> => {
  const { partitionKey, sortKey } = tablesContract().connectors;
  return {
    [partitionKey.name]: { S: executionInput(partitionKey.name) },
    ...(sortKey === null ? {} : { [sortKey.name]: { S: executionInput(sortKey.name) } }),
  };
};

interface RecordDeletionRequest {
  readonly scope: Construct;
  /** Names the three states: Query<name>, Has<name>, Delete<name>Records. */
  readonly name: string;
  readonly table: ITable;
  /** The table's literal name, from `tables.json`, which keys the `BatchWriteItem` request. */
  readonly tableName: string;
  readonly sortKey: string;
  readonly next: IChainable;
  readonly budgetSpent: IChainable;
}

/**
 * Query 25 of the custom connector's keys, delete them, and query again until none are left. The
 * query builds `BatchWriteItem`'s delete requests in its own output, so no Map state runs per record;
 * unprocessed items stay in the table, so the next query returns them again.
 */
const recordDeletion = (request: RecordDeletionRequest): IChainable => {
  const { scope, name, table, tableName } = request;
  const query = sdkCall(scope, `Query${name}`, {
    service: SdkService.DYNAMODB,
    action: "query",
    iamResources: [table.tableArn],
    arguments: {
      TableName: tableName,
      KeyConditionExpression: "connector_arn = :connector_arn",
      ExpressionAttributeValues: { ":connector_arn": { S: executionInput("connector_arn") } },
      // The projection holds only the key, so each item is its own `DeleteRequest.Key`.
      ProjectionExpression: `connector_arn, ${request.sortKey}`,
      Limit: BATCH_WRITE_LIMIT,
    },
    outputs:
      "{% {'Count': $states.result.Count, " +
      "'DeleteRequests': [$states.result.Items.{'DeleteRequest': {'Key': $}}]} %}",
    assign: { passes: ADD_PASS },
  });
  const remove = sdkCall(scope, `Delete${name}Records`, {
    service: SdkService.DYNAMODB,
    action: "batchWriteItem",
    iamResources: [table.tableArn],
    arguments: { RequestItems: { [tableName]: "{% $states.input.DeleteRequests %}" } },
  }).next(query);
  return query.next(
    Choice.jsonata(scope, `Has${name}`)
      .when(passBudgetSpent(), request.budgetSpent)
      .when(Condition.jsonata("{% $states.input.Count > 0 %}"), remove)
      .otherwise(request.next),
  );
};

/**
 * Continue-as-new once the passes are spent: every step treats not found as done, so the new
 * execution skips what is already deleted. The custom connector's `deletion_execution_arn` then
 * names the new execution, so `DeleteCustomConnector` finds it running and starts no other.
 */
const continuation = (props: {
  readonly scope: Construct;
  readonly connectorsTable: ITable;
}): IChainable => {
  const { scope, connectorsTable } = props;
  const continued = Succeed.jsonata(scope, "DeletionContinued");
  const record = sdkCall(scope, "RecordContinuedDeletion", {
    service: SdkService.DYNAMODB,
    action: "updateItem",
    iamResources: [connectorsTable.tableArn],
    arguments: {
      TableName: connectorsTable.tableName,
      Key: connectorKey(),
      UpdateExpression: "SET deletion_execution_arn = :arn",
      ConditionExpression: "attribute_exists(destination_connector_key)",
      ExpressionAttributeValues: {
        ":arn": {
          S:
            "{% $replace($states.context.StateMachine.Id, ':stateMachine:', ':execution:') & ':' & " +
            `${CONTINUED_EXECUTION_NAME} %}`,
        },
      },
    },
    notFound: { errors: [NOT_FOUND.item], next: continued },
  }).next(continued);
  return Choice.jsonata(scope, "CheckContinuations")
    .when(
      continuationsSpent(),
      Fail.jsonata(scope, "DeletionNotFinished", {
        error: "DeletionNotFinished",
        cause: `The deletion ran ${MAX_CONTINUATIONS} continued executions without finishing; call DeleteCustomConnector again.`,
      }),
    )
    .otherwise(
      continueAsNew(scope, "ContinueDeletion", {
        executionName: `{% ${CONTINUED_EXECUTION_NAME} %}`,
        stateMachineArn: STATE_MACHINE_ARN,
      }).next(record),
    );
};

/**
 * `CcfConnectorDeletionWorkflow`'s definition: the schedule, the custom connector's files in the
 * destination bucket (as the tenant's destination role), its document and collection records, its
 * runtime role, one more ingestion for a `BEDROCK` destination, and last the custom connector item.
 * Files go before records. Every catch is not found, which means done.
 */
export const connectorDeletionStates = (
  scope: Construct,
  props: ConnectorDeletionStatesProps,
): IChainable => {
  const { connectorsTable, runtimeRoleArnPattern } = props;
  const budgetSpent = continuation({ scope, connectorsTable });
  const deleteItem = sdkCall(scope, "DeleteConnectorItem", {
    service: SdkService.DYNAMODB,
    action: "deleteItem",
    iamResources: [connectorsTable.tableArn],
    arguments: { TableName: connectorsTable.tableName, Key: connectorKey() },
  });
  const ingestion = props.ingestion.requestIngestion(scope, {
    statePrefix: "Deletion",
    tenantArnPrefix: input("tenant_arn_prefix"),
    destinationId: input("destination_id"),
    next: deleteItem,
  });
  const roleName = { RoleName: executionInput("runtime_role_name") };
  const definitionArn = input("job_definition_arn");
  const deregisterDefinition = sdkCall(scope, "DeregisterJobDefinition", {
    service: SdkService.BATCH,
    action: "deregisterJobDefinition",
    iamResources: [
      `arn:${Aws.PARTITION}:batch:${Aws.REGION}:${Aws.ACCOUNT_ID}:job-definition/${JobExecutionName.DEFINITION_PREFIX}*:*`,
    ],
    arguments: { JobDefinition: `{% ${definitionArn} %}` },
    // Batch answers a revision it doesn't know with a client error; there is nothing left to deregister.
    notFound: { errors: [NOT_FOUND.definition], next: ingestion },
  }).next(ingestion);
  const definition = Choice.jsonata(scope, "HasJobDefinition")
    .when(Condition.jsonata(`{% $type(${definitionArn}) = 'string' %}`), deregisterDefinition)
    .otherwise(ingestion);
  const deleteRole = sdkCall(scope, "DeleteRuntimeRole", {
    service: SdkService.IAM,
    action: "deleteRole",
    iamResources: [runtimeRoleArnPattern],
    arguments: roleName,
    notFound: { errors: [NOT_FOUND.role], next: definition },
  }).next(definition);
  const deleteRolePolicy = sdkCall(scope, "DeleteRuntimeRolePolicy", {
    service: SdkService.IAM,
    action: "deleteRolePolicy",
    iamResources: [runtimeRoleArnPattern],
    arguments: { ...roleName, PolicyName: RuntimeRoleName.POLICY },
    notFound: { errors: [NOT_FOUND.role], next: deleteRole },
  }).next(deleteRole);
  const { documents, collections } = tablesContract();
  const collectionRecords = recordDeletion({
    scope,
    name: "Collections",
    table: props.collectionsTable,
    tableName: collections.name,
    sortKey: "collection_id",
    next: deleteRolePolicy,
    budgetSpent,
  });
  const documentRecords = recordDeletion({
    scope,
    name: "Documents",
    table: props.documentsTable,
    tableName: documents.name,
    sortKey: "collection_document_key",
    next: collectionRecords,
    budgetSpent,
  });
  const tenant: TenantCredentials = {
    roleArn: executionInput("destination_role_arn"),
    roleArns: props.destinationRoleArns,
  };
  const objects = objectDeletionLoop(scope, {
    name: "Connector",
    bucketName: executionInput("bucket_name"),
    prefix: `{% 'connector_id=' & ${input("connector_id")} & '/' %}`,
    tenant,
    done: documentRecords,
    budgetSpent,
    onMissingBucket: documentRecords,
  });
  const deleteSchedule = sdkCall(scope, "DeleteSchedule", {
    service: SdkService.SCHEDULER,
    action: "deleteSchedule",
    iamResources: [
      `arn:${Aws.PARTITION}:scheduler:${Aws.REGION}:${Aws.ACCOUNT_ID}:schedule/${props.scheduleGroupName}/*`,
    ],
    arguments: { Name: executionInput("schedule_name"), GroupName: props.scheduleGroupName },
    notFound: { errors: [NOT_FOUND.schedule], next: objects },
  }).next(objects);
  return startPassCount(scope, "StartPassCount").next(deleteSchedule);
};
