import { Aws } from "aws-cdk-lib";
import { ITable } from "aws-cdk-lib/aws-dynamodb";
import {
  Choice,
  Condition,
  Fail,
  IChainable,
  Pass,
  Succeed,
  Wait,
  WaitTime,
} from "aws-cdk-lib/aws-stepfunctions";
import { CallAwsService } from "aws-cdk-lib/aws-stepfunctions-tasks";
import { Construct } from "constructs";
import { DestinationWorkflowSettings, RESOURCE_PREFIX } from "../common/config";
import { enumMember, tablesContract } from "../common/contracts";
import {
  DestinationRoleName,
  DestinationWorkflowName,
  destinationBucketNameJsonata,
  ingestionLogGroupNameExpression,
} from "../common/destination-names";
import {
  ADD_PASS,
  AdditionalRetry,
  MAX_CONTINUATIONS,
  SdkCallProps,
  SdkService,
  TenantCredentials,
  continueAsNew,
  continuationsSpent,
  executionInput,
  objectDeletionLoop,
  passBudgetSpent,
  sdkCall,
  sdkError,
} from "./workflow-states";

export interface DestinationDeletionStatesProps {
  /** `CcfDestinations`, whose item the deletion records itself in and deletes last. */
  readonly destinationsTable: ITable;
  /** The destination roles the workflow role may assume: `destinationRoleArns(...)`. */
  readonly destinationRoleArns: readonly string[];
  readonly settings: DestinationWorkflowSettings;
}

const DELETING = enumMember({ schema: "DestinationStatus", member: "DELETING" });
/** Bedrock's own status of a knowledge base or data source it is deleting; not the destination's. */
const BEDROCK_DELETING_STATUS = "DELETING";
/** The ingestion job statuses that block `DeleteDataSource`. */
const UNFINISHED_INGESTION_STATUSES = ["STARTING", "IN_PROGRESS", "STOPPING"];
const BEDROCK = SdkService.BEDROCK_AGENT;
const NOT_FOUND = {
  bedrock: sdkError({ service: BEDROCK, errorName: "ResourceNotFoundException" }),
  role: sdkError({ service: SdkService.IAM, errorName: "NoSuchEntityException" }),
  bucket: sdkError({ service: SdkService.S3, errorName: "NoSuchBucketException" }),
  item: sdkError({ service: SdkService.DYNAMODB, errorName: "ConditionalCheckFailedException" }),
  logs: sdkError({ service: SdkService.LOGS, errorName: "ResourceNotFoundException" }),
};
const CORRELATION = `' (correlation ' & $states.context.Execution.Input.correlation_id & ').'`;

/** What every loop of the deletion shares. */
interface DeletionContext {
  readonly scope: Construct;
  readonly tenant: TenantCredentials;
  readonly settings: DestinationWorkflowSettings;
  /** Where a loop goes once the execution's passes are spent. */
  readonly budgetSpent: IChainable;
}

interface TenantCallRequest {
  readonly context: DeletionContext;
  readonly id: string;
  readonly call: Omit<SdkCallProps, "iamResources" | "tenant">;
}

/** An SDK call into the tenant account, as the destination role. */
const tenantCall = ({ context, id, call }: TenantCallRequest): CallAwsService =>
  sdkCall(context.scope, id, { ...call, iamResources: [], tenant: context.tenant });

/** The destination item's key, from the execution input. */
const destinationKey = (): Record<string, unknown> => {
  const { partitionKey, sortKey } = tablesContract().destinations;
  return {
    [partitionKey.name]: { S: executionInput(partitionKey.name) },
    ...(sortKey === null ? {} : { [sortKey.name]: { S: executionInput(sortKey.name) } }),
  };
};

/** A recorded `destination_resources` member, or null. */
const storedResource = (member: string): string =>
  `{% ($value := $states.result.Item.destination_resources.M.${member}.S; $exists($value) ? $value : null) %}`;

interface BedrockResourceDeletionRequest {
  readonly context: DeletionContext;
  /** PascalCase, such as `DataSource`; names the four states and the two variables. */
  readonly name: string;
  /** As the failure cause says it, such as `data source`. */
  readonly label: string;
  readonly arguments: Readonly<Record<string, unknown>>;
  readonly additionalRetry?: AdditionalRetry;
  /** The states that run before the delete call, given the delete call to go to. */
  readonly beforeRemove?: (remove: IChainable) => IChainable;
  readonly next: IChainable;
}

/**
 * Read, check, delete, wait, until Bedrock answers not found. A resource already `DELETING` is
 * waited on with no poll limit; one in another status after its delete fails the execution.
 */
const bedrockResourceDeletion = (request: BedrockResourceDeletionRequest): CallAwsService => {
  const { context, name } = request;
  const stem = `${name.charAt(0).toLowerCase()}${name.slice(1)}`;
  const [status, requested] = [`$${stem}Status`, `$${stem}DeleteRequested`];
  const notFound = { errors: [NOT_FOUND.bedrock], next: request.next };
  const read = tenantCall({
    context,
    id: `Read${name}`,
    call: {
      service: BEDROCK,
      action: `get${name}`,
      arguments: request.arguments,
      assign: { passes: ADD_PASS, [status.slice(1)]: `{% $states.result.${name}.Status %}` },
      notFound,
    },
  });
  const wait = Wait.jsonata(context.scope, `WaitFor${name}Deletion`, {
    time: WaitTime.duration(context.settings.pollInterval),
  }).next(read);
  const remove = tenantCall({
    context,
    id: `Delete${name}`,
    call: {
      service: BEDROCK,
      action: `delete${name}`,
      arguments: request.arguments,
      assign: { [requested.slice(1)]: true },
      additionalRetry: request.additionalRetry,
      notFound,
    },
  }).next(wait);
  const failed = Fail.jsonata(context.scope, `${name}DeletionFailed`, {
    error: `${name}DeletionFailed`,
    cause: `{% 'The ${request.label} deletion ended with status ' & ${status} & ${CORRELATION} %}`,
  });
  const deleting = `${status} = '${BEDROCK_DELETING_STATUS}'`;
  read.next(
    Choice.jsonata(context.scope, `Check${name}Deletion`)
      .when(Condition.jsonata(`{% ${requested} and $not(${deleting}) %}`), failed)
      .when(passBudgetSpent(), context.budgetSpent)
      .when(Condition.jsonata(`{% ${deleting} %}`), wait)
      .otherwise(request.beforeRemove?.(remove) ?? remove),
  );
  return read;
};

interface IngestionStopRequest {
  readonly context: DeletionContext;
  readonly remove: IChainable;
  /** Where a missing data source goes. */
  readonly next: IChainable;
}

/**
 * `DeleteDataSource` fails while an ingestion job runs, and a custom connector's deletion starts
 * one. Each check lists the unfinished jobs, stops the first not yet stopping, and waits; after
 * `maxPolls` checks a job stuck stopping fails the execution.
 */
const ingestionStop = ({ context, remove, next }: IngestionStopRequest): IChainable => {
  const { scope, settings } = context;
  const ids = { KnowledgeBaseId: "{% $knowledgeBaseId %}", DataSourceId: "{% $dataSourceId %}" };
  const list = tenantCall({
    context,
    id: "ListUnfinishedIngestionJobs",
    call: {
      service: BEDROCK,
      action: "listIngestionJobs",
      arguments: {
        ...ids,
        Filters: [{ Attribute: "STATUS", Operator: "EQ", Values: UNFINISHED_INGESTION_STATUSES }],
        MaxResults: settings.ingestionJobsPageSize,
      },
      outputs:
        "{% ($jobs := $states.result.IngestionJobSummaries; {'unfinished': $count($jobs), " +
        "'stoppable': $count($jobs[Status != 'STOPPING']), " +
        "'nextJobId': $jobs[Status != 'STOPPING'][0].IngestionJobId}) %}",
      assign: { passes: ADD_PASS, ingestionChecks: "{% $ingestionChecks + 1 %}" },
      notFound: { errors: [NOT_FOUND.bedrock], next },
    },
  });
  const wait = Wait.jsonata(scope, "WaitForIngestionJobs", {
    time: WaitTime.duration(settings.pollInterval),
  }).next(list);
  const stop = tenantCall({
    context,
    id: "StopIngestionJob",
    call: {
      service: BEDROCK,
      action: "stopIngestionJob",
      arguments: { ...ids, IngestionJobId: "{% $states.input.nextJobId %}" },
      notFound: { errors: [NOT_FOUND.bedrock], next: wait },
    },
  }).next(wait);
  const notStopped = Fail.jsonata(scope, "IngestionJobsNotStopped", {
    error: "IngestionJobsNotStopped",
    cause: `{% 'An ingestion job on the data source was unfinished after ${settings.maxPolls} checks' & ${CORRELATION} %}`,
  });
  return list.next(
    Choice.jsonata(scope, "HasUnfinishedIngestionJobs")
      .when(Condition.jsonata("{% $states.input.unfinished = 0 %}"), remove)
      .when(passBudgetSpent(), context.budgetSpent)
      .when(Condition.jsonata(`{% $ingestionChecks >= ${settings.maxPolls} %}`), notStopped)
      .when(Condition.jsonata("{% $states.input.stoppable > 0 %}"), stop)
      .otherwise(wait),
  );
};

/** Continue-as-new once the passes are spent, and give up after `MAX_CONTINUATIONS`. */
const continuation = (scope: Construct): IChainable =>
  Choice.jsonata(scope, "CheckContinuations")
    .when(
      continuationsSpent(),
      Fail.jsonata(scope, "DeletionNotFinished", {
        error: "DeletionNotFinished",
        cause: `The deletion ran ${MAX_CONTINUATIONS} continued executions without finishing; call DeleteDestination again.`,
      }),
    )
    .otherwise(
      continueAsNew(scope, "ContinueDeletion", {
        executionName:
          "{% $states.context.Execution.Input.execution_name & '-' & " +
          "$string($states.context.Execution.Input.continuation + 1) %}",
        stateMachineArn: `arn:${Aws.PARTITION}:states:${Aws.REGION}:${Aws.ACCOUNT_ID}:stateMachine:${DestinationWorkflowName.DELETION_STATE_MACHINE}`,
      }).next(Succeed.jsonata(scope, "DeletionContinued")),
    );

interface DeletionStepRequest {
  readonly context: DeletionContext;
  readonly next: IChainable;
}

interface LogResourceDeletion {
  readonly id: string;
  readonly action: string;
  readonly arguments: Readonly<Record<string, unknown>>;
  readonly next: IChainable;
}

/**
 * The ingestion log delivery, its source and destination, then its log group, ending at `next`.
 * CloudWatch Logs keeps them after the knowledge base is gone, and a destination without a
 * knowledge base has none, so each one missing means done.
 */
const ingestionLogDeletion = ({ context, next }: DeletionStepRequest): IChainable => {
  const remove = (deletion: LogResourceDeletion): IChainable =>
    tenantCall({
      context,
      id: deletion.id,
      call: {
        service: SdkService.LOGS,
        action: deletion.action,
        arguments: deletion.arguments,
        notFound: { errors: [NOT_FOUND.logs], next: deletion.next },
      },
    }).next(deletion.next);
  const deleteLogGroup = remove({
    id: "DeleteIngestionLogGroup",
    action: "deleteLogGroup",
    arguments: { LogGroupName: "{% $ingestionLogGroupName %}" },
    next,
  });
  const deleteDestination = remove({
    id: "DeleteIngestionLogDestination",
    action: "deleteDeliveryDestination",
    arguments: { Name: "{% $resourceName %}" },
    next: deleteLogGroup,
  });
  const deleteSource = remove({
    id: "DeleteIngestionLogSource",
    action: "deleteDeliverySource",
    arguments: { Name: "{% $resourceName %}" },
    next: deleteDestination,
  });
  const deleteDelivery = remove({
    id: "DeleteIngestionLogDelivery",
    action: "deleteDelivery",
    arguments: { Id: "{% $states.input.deliveryId %}" },
    next: deleteSource,
  });
  return tenantCall({
    context,
    id: "FindIngestionLogDelivery",
    call: {
      service: SdkService.LOGS,
      action: "describeDeliveries",
      arguments: { DeliverySourceNames: ["{% $resourceName %}"] },
      outputs: "{% {'deliveryId': $states.result.Deliveries[0].Id} %}",
    },
  }).next(
    Choice.jsonata(context.scope, "HasIngestionLogDelivery")
      .when(Condition.jsonata("{% $exists($states.input.deliveryId) %}"), deleteDelivery)
      .otherwise(deleteSource),
  );
};

/** The knowledge base role and the bucket, ending at `next`. */
const roleAndBucketDeletion = (request: {
  readonly context: DeletionContext;
  readonly next: IChainable;
}): IChainable => {
  const { context, next } = request;
  const bucket = "{% $bucketName %}";
  const deleteBucket = tenantCall({
    context,
    id: "DeleteBucket",
    call: {
      service: SdkService.S3,
      action: "deleteBucket",
      arguments: { Bucket: bucket },
      notFound: { errors: [NOT_FOUND.bucket], next },
    },
  }).next(next);
  const objects = objectDeletionLoop(context.scope, {
    name: "Bucket",
    bucketName: bucket,
    tenant: context.tenant,
    done: deleteBucket,
    budgetSpent: context.budgetSpent,
    onMissingBucket: next,
  });
  const roleName = { RoleName: "{% $knowledgeBaseRoleName %}" };
  const deleteRole = tenantCall({
    context,
    id: "DeleteKnowledgeBaseRole",
    call: {
      service: SdkService.IAM,
      action: "deleteRole",
      arguments: roleName,
      notFound: { errors: [NOT_FOUND.role], next: objects },
    },
  }).next(objects);
  return tenantCall({
    context,
    id: "DeleteKnowledgeBaseRolePolicy",
    call: {
      service: SdkService.IAM,
      action: "deleteRolePolicy",
      arguments: { ...roleName, PolicyName: DestinationWorkflowName.KNOWLEDGE_BASE_ROLE_POLICY },
      notFound: { errors: [NOT_FOUND.role], next: deleteRole },
    },
  }).next(deleteRole);
};

/**
 * `CcfDestinationDeletionWorkflow`'s definition: the data source, the knowledge base, the
 * knowledge base role, the bucket's objects, the bucket, then the item. Every not-found error
 * means done. The two item writes are conditional on `DELETING`, so a quota cleanup skips both.
 */
export const destinationDeletionStates = (
  scope: Construct,
  props: DestinationDeletionStatesProps,
): IChainable => {
  const { destinationsTable: table, settings } = props;
  const tenant = { roleArn: executionInput("role_arn"), roleArns: props.destinationRoleArns };
  const context = { scope, tenant, settings, budgetSpent: continuation(scope) };
  const item = { service: SdkService.DYNAMODB, iamResources: [table.tableArn] };
  const itemWhileDeleting = {
    TableName: table.tableName,
    Key: destinationKey(),
    ConditionExpression: "#status = :deleting",
    ExpressionAttributeNames: { "#status": "status" },
  };
  const deleteItem = sdkCall(scope, "DeleteDestinationItem", {
    ...item,
    action: "deleteItem",
    arguments: {
      ...itemWhileDeleting,
      ExpressionAttributeValues: { ":deleting": { S: DELETING } },
    },
    notFound: { errors: [NOT_FOUND.item], next: Succeed.jsonata(scope, "StatusChangedElsewhere") },
  });
  const roleAndBucket = ingestionLogDeletion({
    context,
    next: roleAndBucketDeletion({ context, next: deleteItem }),
  });
  const knowledgeBaseId = { KnowledgeBaseId: "{% $knowledgeBaseId %}" };
  const knowledgeBase = bedrockResourceDeletion({
    context,
    name: "KnowledgeBase",
    label: "knowledge base",
    arguments: knowledgeBaseId,
    next: roleAndBucket,
  });
  const dataSource = bedrockResourceDeletion({
    context,
    name: "DataSource",
    label: "data source",
    arguments: { ...knowledgeBaseId, DataSourceId: "{% $dataSourceId %}" },
    additionalRetry: {
      errors: [sdkError({ service: BEDROCK, errorName: "ValidationException" })],
      ...settings.dataSourceBusyRetry,
    },
    beforeRemove: (remove) => ingestionStop({ context, remove, next: knowledgeBase }),
    next: knowledgeBase,
  });
  const readDestination = sdkCall(scope, "ReadDestination", {
    ...item,
    action: "getItem",
    arguments: {
      TableName: table.tableName,
      Key: destinationKey(),
      ConsistentRead: true,
      ProjectionExpression: "destination_resources",
    },
    assign: {
      knowledgeBaseId: storedResource("knowledge_base_id"),
      dataSourceId: storedResource("data_source_id"),
    },
  }).next(
    Choice.jsonata(scope, "ChooseFirstResource")
      .when(
        Condition.jsonata("{% $dataSourceId != null and $knowledgeBaseId != null %}"),
        dataSource,
      )
      .when(Condition.jsonata("{% $knowledgeBaseId != null %}"), knowledgeBase)
      .otherwise(roleAndBucket),
  );
  const recordExecution = sdkCall(scope, "RecordDeletionExecution", {
    ...item,
    action: "updateItem",
    arguments: {
      ...itemWhileDeleting,
      UpdateExpression: "SET workflow_execution_arn = :arn",
      ExpressionAttributeValues: {
        ":arn": { S: "{% $states.context.Execution.Id %}" },
        ":deleting": { S: DELETING },
      },
    },
    notFound: { errors: [NOT_FOUND.item], next: readDestination },
  }).next(readDestination);
  return Pass.jsonata(scope, "ResolveNames", {
    assign: {
      passes: 0,
      ingestionChecks: 0,
      dataSourceDeleteRequested: false,
      knowledgeBaseDeleteRequested: false,
      bucketName: destinationBucketNameJsonata({
        destinationId: "$states.input.destination_id",
        account: "$split($states.input.tenant_arn_prefix, ':')[4]",
        region: `'${Aws.REGION}'`,
      }),
      knowledgeBaseRoleName: `{% '${DestinationRoleName.KNOWLEDGE_BASE_ROLE_PREFIX}' & $states.input.destination_id %}`,
      resourceName: `{% '${RESOURCE_PREFIX}' & $states.input.destination_id %}`,
      ingestionLogGroupName: `{% ${ingestionLogGroupNameExpression("$states.input.destination_id")} %}`,
    },
  }).next(recordExecution);
};
