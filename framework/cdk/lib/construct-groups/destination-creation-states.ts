import { Aws } from "aws-cdk-lib";
import { ITable } from "aws-cdk-lib/aws-dynamodb";
import { RetentionDays } from "aws-cdk-lib/aws-logs";
import {
  Choice,
  Condition,
  Errors,
  Fail,
  IChainable,
  IStateMachine,
  Pass,
  Succeed,
  TaskStateBase,
  Wait,
  WaitTime,
} from "aws-cdk-lib/aws-stepfunctions";
import { Construct } from "constructs";
import { DestinationWorkflowSettings, RESOURCE_PREFIX } from "../common/config";
import {
  enumMember,
  frameworkMetricName,
  maxLength,
  metricsContract,
  tablesContract,
} from "../common/contracts";
import { destinationBucketPolicy } from "../common/destination-bucket-policy";
import {
  DESTINATION_ID_TAG,
  DestinationRoleName,
  DestinationWorkflowAction,
  DestinationWorkflowName,
  KNOWLEDGE_BASE_LOG_TYPE,
  KNOWLEDGE_BASE_METRIC_NAMESPACE,
  destinationBucketNameJsonata,
  ingestionLogGroupNameExpression,
} from "../common/destination-names";
import { SdkCallProps, SdkService, childExecution, sdkCall, sdkError } from "./workflow-states";

export interface DestinationCreationStatesProps {
  /** `CcfDestinations`, whose status and resources the create path writes. */
  readonly destinationsTable: ITable;
  /** The destination roles the workflow role may assume: `destinationRoleArns(...)`. */
  readonly destinationRoleArns: readonly string[];
  /** `CcfDestinationDeletionWorkflow`, run for a delete and for the quota cleanup. */
  readonly deletionStateMachine: IStateMachine;
  /** The service Region at synthesis; S3 rejects a location constraint in `us-east-1`. */
  readonly region: string;
  /** The data source's deletion protection threshold, 0 to 100. */
  readonly deletionProtectionThreshold: number;
  /** How long the knowledge base's ingestion log group keeps its events. */
  readonly ingestionLogRetention: RetentionDays;
  readonly settings: DestinationWorkflowSettings;
}

/** The execution input, which every state reads; a state's own input changes after a catch. */
const INPUT = "$states.context.Execution.Input";
const DEFAULT_S3_REGION = "us-east-1";
const status = (member: string): string => enumMember({ schema: "DestinationStatus", member });
const CREATING = ["CREATING_BUCKET", "CREATING_KNOWLEDGE_BASE", "CREATING_DATA_SOURCE"].map(status);
const CONDITION_FAILED = sdkError({
  service: SdkService.DYNAMODB,
  errorName: "ConditionalCheckFailedException",
});
/** Errors that mean the tenant account reached a quota; the cleanup removes what the create made. */
export const QUOTA_ERRORS = [
  sdkError({ service: SdkService.S3, errorName: "TooManyBucketsException" }),
  sdkError({ service: SdkService.IAM, errorName: "LimitExceededException" }),
  sdkError({ service: SdkService.BEDROCK_AGENT, errorName: "ServiceQuotaExceededException" }),
];
/** The input members the deletion child reads. */
const CHILD_INPUT_MEMBERS = [
  "destination_id",
  "tenant_arn_prefix",
  "role_arn",
  "type",
  "access_control_enabled",
  "correlation_id",
  "api_request_id",
  "execution_name",
  "continuation",
  "action",
];
const REASON_TAIL_LENGTH = 128;
const REASON_ELISION = " ... ";
const CORRELATION = `' (correlation ' & ${INPUT}.correlation_id & ')'`;
const BUCKET_ARN = `'arn:${Aws.PARTITION}:s3:::' & $bucketName`;
const FAILURE = { errors: [Errors.ALL], assign: { error: "{% $states.errorOutput %}" } };

/**
 * `status_reason`: `<Error>: <Cause>`, the cleanup error when there is one, and the correlation ID.
 * A reason over the model's limit keeps its start, `" ... "`, and its last 128 characters.
 */
export const failureReason = (): string => {
  const limit = maxLength({ schema: "Destination", member: "status_reason" });
  const head = limit - REASON_ELISION.length - REASON_TAIL_LENGTH;
  return (
    `{% ($reason := $error.Error & ': ' & $error.Cause & ` +
    `($cleanupError ? '; cleanup incomplete: ' & $cleanupError : '') & ${CORRELATION}; ` +
    `$length($reason) > ${limit} ? $substring($reason, 0, ${head}) & '${REASON_ELISION}' & ` +
    `$substring($reason, $length($reason) - ${REASON_TAIL_LENGTH}) : $reason) %}`
  );
};

/** A failed child's error name from its `States.TaskFailed` cause, or the task's own error, such as a timeout. */
export const CLEANUP_ERROR =
  "{% $states.errorOutput.Error = 'States.TaskFailed' ? " +
  "($cause := $parse($states.errorOutput.Cause); $cause.Error ? $cause.Error : $cause.error) : " +
  "$states.errorOutput.Error %}";

/** The data source's S3 connector parameters; the ACL members only with access control on. */
const CONNECTOR_PARAMETERS =
  "{% $merge([{'type': 'S3', 'version': '1', " +
  "'connectionConfiguration': {'bucketName': $bucketName, 'bucketOwnerAccountId': $account}, " +
  `'filterConfiguration': {'exclusionPrefixes': ['${DestinationWorkflowName.ACL_PREFIX}']}, ` +
  `'aclEnabled': ${INPUT}.access_control_enabled}, ${INPUT}.access_control_enabled ? ` +
  "{'aclConfiguration': {'globalAccessControlListS3Uri': $aclFileUri}} : {}]) %}";

const input = (member: string): string => `{% ${INPUT}.${member} %}`;

const childInput = (overrides: Readonly<Record<string, unknown>>): Record<string, unknown> => ({
  ...Object.fromEntries(CHILD_INPUT_MEMBERS.map((member) => [member, input(member)])),
  ...overrides,
});

/** The documents the create path writes, with JSONata leaves; `RenderDocuments` assigns them. */
const documents = () => ({
  bucketPolicy: destinationBucketPolicy({
    bucketArn: `{% ${BUCKET_ARN} %}`,
    objectsArn: `{% ${BUCKET_ARN} & '/*' %}`,
  }),
  trustPolicy: {
    Version: "2012-10-17",
    Statement: [
      {
        Effect: "Allow",
        Principal: { Service: "bedrock.amazonaws.com" },
        Action: "sts:AssumeRole",
        Condition: {
          StringEquals: { "aws:SourceAccount": "{% $account %}" },
          ArnLike: {
            "aws:SourceArn": `{% 'arn:${Aws.PARTITION}:bedrock:${Aws.REGION}:' & $account & ':knowledge-base/*' %}`,
          },
        },
      },
    ],
  },
  readPolicy: {
    Version: "2012-10-17",
    Statement: [
      {
        Effect: "Allow",
        Action: ["s3:GetObject*", "s3:GetBucket*", "s3:List*"],
        Resource: [`{% ${BUCKET_ARN} %}`, `{% ${BUCKET_ARN} & '/*' %}`],
      },
      // Bedrock publishes the knowledge base's Retrieve metrics as this role; PutMetricData takes no resource.
      {
        Effect: "Allow",
        Action: "cloudwatch:PutMetricData",
        Resource: "*",
        Condition: { StringEquals: { "cloudwatch:namespace": KNOWLEDGE_BASE_METRIC_NAMESPACE } },
      },
    ],
  },
  bucketArn: `{% ${BUCKET_ARN} %}`,
  knowledgeBaseRoleArn: `{% 'arn:${Aws.PARTITION}:iam::' & $account & ':role${DestinationRoleName.PATH}' & $knowledgeBaseRoleName %}`,
  aclFileUri: `{% 's3://' & $bucketName & '/${DestinationWorkflowName.GLOBAL_ACL_KEY}' %}`,
});

interface CreationContext {
  readonly scope: Construct;
  readonly props: DestinationCreationStatesProps;
  /** Where a failed call goes, with the error in `$error`. */
  readonly recordFailure: IChainable;
  readonly changedElsewhere: IChainable;
}

interface CreateCallRequest {
  readonly context: CreationContext;
  readonly id: string;
  readonly call: Omit<SdkCallProps, "iamResources" | "tenant">;
}

/** A call into the tenant account as the destination role; any error but `notFound`'s records a failure. */
const createCall = ({ context, id, call }: CreateCallRequest): TaskStateBase =>
  sdkCall(context.scope, id, {
    ...call,
    iamResources: [],
    tenant: { roleArn: input("role_arn"), roleArns: context.props.destinationRoleArns },
  }).addCatch(context.recordFailure, FAILURE);

/** The destination item's key, from the execution input. */
const destinationKey = (): Record<string, unknown> => {
  const { partitionKey, sortKey } = tablesContract().destinations;
  return {
    [partitionKey.name]: { S: input(partitionKey.name) },
    ...(sortKey === null ? {} : { [sortKey.name]: { S: input(sortKey.name) } }),
  };
};

interface StatusUpdateRequest {
  readonly context: CreationContext;
  readonly id: string;
  readonly from: string;
  /** Absent: the update records resources and keeps the status. */
  readonly to?: string;
  /** `destination_resources` members and the JSONata expression of each value. */
  readonly resources: Readonly<Record<string, string>>;
}

/** A conditional update from `from`; a failed condition means another execution changed the item. */
const statusUpdate = (request: StatusUpdateRequest): TaskStateBase => {
  const { context } = request;
  const table = context.props.destinationsTable;
  const resources = Object.entries(request.resources).map(
    ([name, value]): [string, { S: string }] => [name, { S: value }],
  );
  const moves = request.to !== undefined;
  return sdkCall(context.scope, request.id, {
    service: SdkService.DYNAMODB,
    action: "updateItem",
    iamResources: [table.tableArn],
    arguments: {
      TableName: table.tableName,
      Key: destinationKey(),
      UpdateExpression: `SET ${moves ? "#status = :next, " : ""}destination_resources = :resources`,
      ConditionExpression: "#status = :expected",
      ExpressionAttributeNames: { "#status": "status" },
      ExpressionAttributeValues: {
        ":expected": { S: request.from },
        ":resources": { M: Object.fromEntries(resources) },
        ...(moves ? { ":next": { S: request.to } } : {}),
      },
    },
    notFound: { errors: [CONDITION_FAILED], next: context.changedElsewhere },
  }).addCatch(context.recordFailure, FAILURE);
};

const BUCKET_RESOURCES = { bucket_arn: "{% $bucketArn %}", bucket_name: "{% $bucketName %}" };
const KNOWLEDGE_BASE_RESOURCES = {
  ...BUCKET_RESOURCES,
  knowledge_base_arn: "{% $knowledgeBaseArn %}",
  knowledge_base_id: "{% $knowledgeBaseId %}",
  knowledge_base_role_arn: "{% $knowledgeBaseRoleArn %}",
};

/** The failure path: the quota cleanup, `FAILED` with the reason, the metric, and the failed execution. */
const failurePath = ({
  scope,
  props,
}: {
  readonly scope: Construct;
  readonly props: DestinationCreationStatesProps;
}) => {
  const changedElsewhere = Succeed.jsonata(scope, "StatusChangedElsewhere");
  const table = props.destinationsTable;
  const creationFailed = Fail.jsonata(scope, "CreationFailed", {
    error: "{% $error.Error %}",
    cause: `{% $error.Cause & ${CORRELATION} %}`,
  });
  const publish = sdkCall(scope, "PublishCreationFailed", {
    service: SdkService.CLOUDWATCH,
    action: "putMetricData",
    iamResources: ["*"],
    arguments: {
      Namespace: metricsContract().namespace,
      MetricData: [
        { MetricName: frameworkMetricName("DESTINATION_CREATION_FAILED"), Value: 1, Unit: "Count" },
      ],
    },
  }).next(creationFailed);
  const values = Object.fromEntries(
    CREATING.map((member, index) => [`:creating${index}`, { S: member }]),
  );
  const markFailed = sdkCall(scope, "MarkFailed", {
    service: SdkService.DYNAMODB,
    action: "updateItem",
    iamResources: [table.tableArn],
    arguments: {
      TableName: table.tableName,
      Key: destinationKey(),
      UpdateExpression: "SET #status = :failed, status_reason = :reason",
      ConditionExpression: `#status IN (${Object.keys(values).join(", ")})`,
      ExpressionAttributeNames: { "#status": "status" },
      ExpressionAttributeValues: {
        ...values,
        ":failed": { S: status("FAILED") },
        ":reason": { S: failureReason() },
      },
    },
    notFound: { errors: [CONDITION_FAILED], next: changedElsewhere },
  }).next(publish);
  const cleanUp = childExecution(scope, "CleanUpAfterQuotaError", {
    stateMachine: props.deletionStateMachine,
    input: childInput({
      action: DestinationWorkflowAction.DELETE,
      execution_name: `{% ${INPUT}.execution_name & '-cleanup' %}`,
      continuation: 0,
    }),
    executionName: `{% ${INPUT}.execution_name & '-cleanup' %}`,
    timeout: props.settings.quotaCleanupTimeout,
  });
  cleanUp.addCatch(markFailed, { errors: [Errors.ALL], assign: { cleanupError: CLEANUP_ERROR } });
  const quota = QUOTA_ERRORS.map((error) => `'${error}'`).join(", ");
  const recordFailure = Choice.jsonata(scope, "RecordFailure")
    .when(Condition.jsonata(`{% $error.Error in [${quota}] %}`), cleanUp.next(markFailed))
    .otherwise(markFailed);
  return { recordFailure, changedElsewhere };
};

interface IngestionLogDeliveryRequest {
  readonly context: CreationContext;
  readonly next: IChainable;
}

/**
 * The knowledge base's ingestion logs, which Bedrock records per document: a log group in the
 * tenant account, a delivery source for the knowledge base, a delivery destination for the log
 * group, and the delivery between them. Each step tolerates a rerun.
 */
const ingestionLogDelivery = ({ context, next }: IngestionLogDeliveryRequest): IChainable => {
  const logs = SdkService.LOGS;
  const already = (errorName: string, then: IChainable) => ({
    errors: [sdkError({ service: logs, errorName })],
    next: then,
  });
  const createDelivery = createCall({
    context,
    id: "CreateIngestionLogDelivery",
    call: {
      service: logs,
      action: "createDelivery",
      arguments: {
        DeliverySourceName: "{% $resourceName %}",
        DeliveryDestinationArn: "{% $deliveryDestinationArn %}",
      },
      notFound: already("ConflictException", next),
    },
  }).next(next);
  const putDestination = createCall({
    context,
    id: "PutIngestionLogDestination",
    call: {
      service: logs,
      action: "putDeliveryDestination",
      arguments: {
        Name: "{% $resourceName %}",
        OutputFormat: "json",
        DeliveryDestinationConfiguration: { DestinationResourceArn: "{% $ingestionLogGroupArn %}" },
      },
      assign: { deliveryDestinationArn: "{% $states.result.DeliveryDestination.Arn %}" },
    },
  }).next(createDelivery);
  const putSource = createCall({
    context,
    id: "PutIngestionLogSource",
    call: {
      service: logs,
      action: "putDeliverySource",
      arguments: {
        Name: "{% $resourceName %}",
        ResourceArn: "{% $knowledgeBaseArn %}",
        LogType: KNOWLEDGE_BASE_LOG_TYPE,
      },
    },
  }).next(putDestination);
  const putRetention = createCall({
    context,
    id: "PutIngestionLogRetention",
    call: {
      service: logs,
      action: "putRetentionPolicy",
      arguments: {
        LogGroupName: "{% $ingestionLogGroupName %}",
        RetentionInDays: context.props.ingestionLogRetention,
      },
    },
  }).next(putSource);
  return createCall({
    context,
    id: "CreateIngestionLogGroup",
    call: {
      service: logs,
      action: "createLogGroup",
      arguments: {
        LogGroupName: "{% $ingestionLogGroupName %}",
        Tags: { [DESTINATION_ID_TAG]: input("destination_id") },
      },
      notFound: already("ResourceAlreadyExistsException", putRetention),
    },
  }).next(putRetention);
};

/** `BEDROCK` only: the knowledge base role, the knowledge base, the ACL file, and the data source. */
const knowledgeBasePath = (context: CreationContext): IChainable => {
  const { scope, props } = context;
  const { settings } = props;
  const bedrock = SdkService.BEDROCK_AGENT;
  const tags = [{ Key: DESTINATION_ID_TAG, Value: input("destination_id") }];
  const setActive = statusUpdate({
    context,
    id: "SetActive",
    from: status("CREATING_DATA_SOURCE"),
    to: status("ACTIVE"),
    resources: { ...KNOWLEDGE_BASE_RESOURCES, data_source_id: "{% $dataSourceId %}" },
  });
  const createDataSource = createCall({
    context,
    id: "CreateDataSource",
    call: {
      service: bedrock,
      action: "createDataSource",
      arguments: {
        KnowledgeBaseId: "{% $knowledgeBaseId %}",
        Name: "{% $resourceName %}",
        ClientToken: "{% $dataSourceToken %}",
        DataDeletionPolicy: "DELETE",
        DataSourceConfiguration: {
          Type: "MANAGED_KNOWLEDGE_BASE_CONNECTOR",
          ManagedKnowledgeBaseConnectorConfiguration: {
            ConnectorParameters: CONNECTOR_PARAMETERS,
            DeletionProtectionConfiguration: {
              DeletionProtectionStatus: "ENABLED",
              DeletionProtectionThreshold: props.deletionProtectionThreshold,
            },
          },
        },
      },
      assign: { dataSourceId: "{% $states.result.DataSource.DataSourceId %}" },
    },
  }).next(ingestionLogDelivery({ context, next: setActive }));
  const writeAclFile = createCall({
    context,
    id: "WriteGlobalAclFile",
    call: {
      service: SdkService.S3,
      action: "putObject",
      arguments: {
        Bucket: "{% $bucketName %}",
        Key: DestinationWorkflowName.GLOBAL_ACL_KEY,
        Body: "[]",
        ContentType: "application/json",
      },
    },
  }).next(createDataSource);
  const setCreatingDataSource = statusUpdate({
    context,
    id: "SetCreatingDataSource",
    from: status("CREATING_KNOWLEDGE_BASE"),
    to: status("CREATING_DATA_SOURCE"),
    resources: KNOWLEDGE_BASE_RESOURCES,
  }).next(
    Choice.jsonata(scope, "ChooseAccessControl")
      .when(Condition.jsonata(`{% ${INPUT}.access_control_enabled %}`), writeAclFile)
      .otherwise(createDataSource),
  );
  const wait = Wait.jsonata(scope, "WaitForKnowledgeBase", {
    time: WaitTime.duration(settings.pollInterval),
  });
  const timedOut = `${settings.maxPolls} checks ${settings.pollInterval.toSeconds()} seconds apart`;
  const check = Choice.jsonata(scope, "CheckKnowledgeBase")
    .when(Condition.jsonata("{% $knowledgeBaseStatus = 'ACTIVE' %}"), setCreatingDataSource)
    .when(Condition.jsonata("{% $knowledgeBaseStatus = 'FAILED' %}"), context.recordFailure, {
      assign: {
        error: {
          Error: "KnowledgeBaseFailed",
          Cause: "Bedrock reported the knowledge base as FAILED",
        },
      },
    })
    .when(Condition.jsonata(`{% $polls >= ${settings.maxPolls} %}`), context.recordFailure, {
      assign: {
        error: {
          Error: "KnowledgeBaseTimedOut",
          Cause: `The knowledge base wasn't ACTIVE after ${timedOut}`,
        },
      },
    })
    .otherwise(wait);
  wait.next(
    createCall({
      context,
      id: "GetKnowledgeBase",
      call: {
        service: bedrock,
        action: "getKnowledgeBase",
        arguments: { KnowledgeBaseId: "{% $knowledgeBaseId %}" },
        assign: {
          knowledgeBaseStatus: "{% $states.result.KnowledgeBase.Status %}",
          polls: "{% $polls + 1 %}",
        },
      },
    }).next(check),
  );
  const createKnowledgeBase = createCall({
    context,
    id: "CreateKnowledgeBase",
    call: {
      service: bedrock,
      action: "createKnowledgeBase",
      arguments: {
        Name: "{% $resourceName %}",
        RoleArn: "{% $knowledgeBaseRoleArn %}",
        KnowledgeBaseConfiguration: {
          Type: "MANAGED",
          ManagedKnowledgeBaseConfiguration: { EmbeddingModelType: "MANAGED" },
        },
        Tags: { [DESTINATION_ID_TAG]: input("destination_id") },
        ClientToken: "{% $knowledgeBaseToken %}",
      },
      assign: {
        knowledgeBaseId: "{% $states.result.KnowledgeBase.KnowledgeBaseId %}",
        knowledgeBaseArn: "{% $states.result.KnowledgeBase.KnowledgeBaseArn %}",
      },
    },
  }).next(
    statusUpdate({
      context,
      id: "RecordKnowledgeBase",
      from: status("CREATING_KNOWLEDGE_BASE"),
      resources: KNOWLEDGE_BASE_RESOURCES,
    }).next(wait),
  );
  const putRolePolicy = createCall({
    context,
    id: "PutKnowledgeBaseRolePolicy",
    call: {
      service: SdkService.IAM,
      action: "putRolePolicy",
      arguments: {
        RoleName: "{% $knowledgeBaseRoleName %}",
        PolicyName: DestinationWorkflowName.KNOWLEDGE_BASE_ROLE_POLICY,
        PolicyDocument: "{% $string($readPolicy) %}",
      },
    },
  }).next(
    Wait.jsonata(scope, "WaitForRolePropagation", {
      time: WaitTime.duration(settings.rolePropagation),
    }).next(createKnowledgeBase),
  );
  const createRole = createCall({
    context,
    id: "CreateKnowledgeBaseRole",
    call: {
      service: SdkService.IAM,
      action: "createRole",
      arguments: {
        RoleName: "{% $knowledgeBaseRoleName %}",
        Path: DestinationRoleName.PATH,
        AssumeRolePolicyDocument: "{% $string($trustPolicy) %}",
        PermissionsBoundary: input("boundary_arn"),
        Tags: tags,
      },
      notFound: {
        errors: [sdkError({ service: SdkService.IAM, errorName: "EntityAlreadyExistsException" })],
        next: putRolePolicy,
      },
    },
  }).next(putRolePolicy);
  return statusUpdate({
    context,
    id: "SetCreatingKnowledgeBase",
    from: status("CREATING_BUCKET"),
    to: status("CREATING_KNOWLEDGE_BASE"),
    resources: BUCKET_RESOURCES,
  }).next(createRole);
};

/** The bucket and its hardening, then `ACTIVE` or, for `BEDROCK`, the knowledge base path. */
const bucketPath = (context: CreationContext): IChainable => {
  const { scope, props } = context;
  const bucket = { Bucket: "{% $bucketName %}" };
  const hardening: Readonly<Record<string, { action: string; arguments: object }>> = {
    BlockPublicAccess: {
      action: "putPublicAccessBlock",
      arguments: {
        PublicAccessBlockConfiguration: {
          BlockPublicAcls: true,
          IgnorePublicAcls: true,
          BlockPublicPolicy: true,
          RestrictPublicBuckets: true,
        },
      },
    },
    EncryptBucket: {
      action: "putBucketEncryption",
      arguments: {
        ServerSideEncryptionConfiguration: {
          Rules: [{ ApplyServerSideEncryptionByDefault: { SseAlgorithm: "AES256" } }],
        },
      },
    },
    TagBucket: {
      action: "putBucketTagging",
      arguments: {
        Tagging: { TagSet: [{ Key: DESTINATION_ID_TAG, Value: input("destination_id") }] },
      },
    },
    PutBucketPolicy: {
      action: "putBucketPolicy",
      arguments: { Policy: "{% $string($bucketPolicy) %}" },
    },
  };
  const steps = Object.entries(hardening).map(([id, step]) =>
    createCall({
      context,
      id,
      call: {
        service: SdkService.S3,
        action: step.action,
        arguments: { ...bucket, ...step.arguments },
      },
    }),
  );
  steps.forEach((step, index) => {
    if (index > 0) steps[index - 1].next(step);
  });
  const [blockPublicAccess] = steps;
  steps[steps.length - 1].next(
    Choice.jsonata(scope, "ChooseDestinationType")
      .when(
        Condition.jsonata(
          `{% ${INPUT}.type = '${enumMember({ schema: "DestinationType", member: "BEDROCK" })}' %}`,
        ),
        knowledgeBasePath(context),
      )
      .otherwise(
        statusUpdate({
          context,
          id: "SetActiveWithoutKnowledgeBase",
          from: status("CREATING_BUCKET"),
          to: status("ACTIVE"),
          resources: BUCKET_RESOURCES,
        }),
      ),
  );
  return createCall({
    context,
    id: "CreateBucket",
    call: {
      service: SdkService.S3,
      action: "createBucket",
      arguments: {
        ...bucket,
        ...(props.region === DEFAULT_S3_REGION
          ? {}
          : { CreateBucketConfiguration: { LocationConstraint: props.region } }),
      },
      notFound: {
        errors: [
          sdkError({ service: SdkService.S3, errorName: "BucketAlreadyOwnedByYouException" }),
        ],
        next: blockPublicAccess,
      },
    },
  }).next(blockPublicAccess);
};

/**
 * `CcfDestinationWorkflow`'s definition. It records its own execution, then runs the deletion
 * child for `DELETE`, or creates the bucket and, for `BEDROCK`, the knowledge base and data source.
 * A failed create records `FAILED` once, after the deletion child cleans up a quota error.
 */
export const destinationCreationStates = (
  scope: Construct,
  props: DestinationCreationStatesProps,
): IChainable => {
  const table = props.destinationsTable;
  const { recordFailure, changedElsewhere } = failurePath({ scope, props });
  const context = { scope, props, recordFailure, changedElsewhere };
  const deleteDestination = childExecution(scope, "DeleteDestination", {
    stateMachine: props.deletionStateMachine,
    input: childInput({}),
    executionName: input("execution_name"),
  });
  deleteDestination.addCatch(
    Fail.jsonata(scope, "DeletionFailed", {
      error: "{% $states.input.Error %}",
      cause: "{% $states.input.Cause %}",
    }),
    { errors: [Errors.ALL] },
  );
  const render = Pass.jsonata(scope, "RenderDocuments", { assign: documents() }).next(
    bucketPath(context),
  );
  const { partitionKey } = tablesContract().destinations;
  const recordExecution = sdkCall(scope, "RecordExecution", {
    service: SdkService.DYNAMODB,
    action: "updateItem",
    iamResources: [table.tableArn],
    arguments: {
      TableName: table.tableName,
      Key: destinationKey(),
      UpdateExpression: "SET workflow_execution_arn = :arn",
      ConditionExpression: `attribute_exists(${partitionKey.name})`,
      ExpressionAttributeValues: { ":arn": { S: "{% $states.context.Execution.Id %}" } },
    },
    notFound: { errors: [CONDITION_FAILED], next: changedElsewhere },
  }).next(
    Choice.jsonata(scope, "ChooseAction")
      .when(
        Condition.jsonata(`{% ${INPUT}.action = '${DestinationWorkflowAction.DELETE}' %}`),
        deleteDestination,
      )
      .when(
        Condition.jsonata(`{% ${INPUT}.action = '${DestinationWorkflowAction.CREATE}' %}`),
        render,
      ),
  );
  const id = `${INPUT}.destination_id`;
  return Pass.jsonata(scope, "ResolveNames", {
    assign: {
      polls: 0,
      cleanupError: null,
      account: `{% $split(${INPUT}.tenant_arn_prefix, ':')[4] %}`,
      bucketName: destinationBucketNameJsonata({
        destinationId: id,
        account: `$split(${INPUT}.tenant_arn_prefix, ':')[4]`,
        region: `'${Aws.REGION}'`,
      }),
      knowledgeBaseRoleName: `{% '${DestinationRoleName.KNOWLEDGE_BASE_ROLE_PREFIX}' & ${id} %}`,
      resourceName: `{% '${RESOURCE_PREFIX}' & ${id} %}`,
      ingestionLogGroupName: `{% ${ingestionLogGroupNameExpression(id)} %}`,
      ingestionLogGroupArn: `{% 'arn:${Aws.PARTITION}:logs:${Aws.REGION}:' & $split(${INPUT}.tenant_arn_prefix, ':')[4] & ':log-group:' & ${ingestionLogGroupNameExpression(id)} %}`,
      knowledgeBaseToken: `{% $hash('knowledge-base-' & ${id}, 'SHA-256') %}`,
      dataSourceToken: `{% $hash('data-source-' & ${id}, 'SHA-256') %}`,
    },
  }).next(recordExecution);
};
