import { Duration, RemovalPolicy } from "aws-cdk-lib";
import {
  Effect,
  IGrantable,
  ManagedPolicy,
  PolicyStatement,
  Role,
  ServicePrincipal,
} from "aws-cdk-lib/aws-iam";
import { IKey } from "aws-cdk-lib/aws-kms";
import { Function as LambdaFunction } from "aws-cdk-lib/aws-lambda";
import { SqsEventSource } from "aws-cdk-lib/aws-lambda-event-sources";
import { RetentionDays } from "aws-cdk-lib/aws-logs";
import { Queue } from "aws-cdk-lib/aws-sqs";
import { IChainable } from "aws-cdk-lib/aws-stepfunctions";
import { Construct } from "constructs";
import {
  IngestionQueueName,
  IngestionQueueSettings,
  ProjectName,
  ROLE_PATH,
  ResourceName,
} from "../common/config";
import { enumMember, metricsContract, tablesContract } from "../common/contracts";
import { destinationRoleArns } from "../common/destination-names";
import {
  acknowledgeNagRules,
  actionsWithoutResourceAcknowledgment,
  assumeDestinationRolesAcknowledgment,
  XRAY_TRACE_ACTIONS,
} from "../common/nag-suppressions";
import { BackendFunction } from "../constructs/backend-function";
import { ServiceQueue } from "../constructs/service-queue";
import { BackendBundle } from "./backend-bundle";
import { DatabaseTables } from "./database-tables";
import { handlerSettingsEnvironment } from "./handler-settings";
import { SdkService, sdkCall, sdkError } from "./workflow-states";

/** Six times the consumer's timeout, as Lambda recommends for an SQS event source. */
const VISIBILITY_TIMEOUT_FACTOR = 6;
const SETTINGS_KEY = "SERVICE_SETTINGS.ingestionQueue.retryDelaysSeconds";

export interface IngestionQueueProps {
  readonly tables: DatabaseTables;
  readonly serviceKey: IKey;
  readonly serviceAccountId: string;
  readonly allowedTenantAccountIds: readonly string[];
  /** `CcfDestinationWorkflow`'s ARN, built from its fixed name: every `CcfDestinationRole` session sends it as the external ID. */
  readonly destinationWorkflowArn: string;
  /** `CcfConnectorDeletionWorkflow`'s ARN, built from its fixed name; that workflow sends to this queue, so it can't be a reference. */
  readonly connectorDeletionWorkflowArn: string;
  readonly bundle: BackendBundle;
  readonly memorySize: number;
  readonly timeout: Duration;
  /** How long both queues keep a message. */
  readonly retention: Duration;
  readonly settings: IngestionQueueSettings;
  readonly logRetention: RetentionDays;
  readonly removalPolicy: RemovalPolicy;
}

/** Where a workflow's ingestion request comes from; each value is a bare JSONata expression, without `{% %}`. */
export interface RequestIngestionProps {
  /** Prefixes both state names, so one state machine can use the builder twice. */
  readonly statePrefix: string;
  readonly tenantArnPrefix: string;
  readonly destinationId: string;
  /** Where the chain continues, for every outcome. */
  readonly next: IChainable;
}

/**
 * `CcfIngestionQueue`, its dead-letter queue, and the ingestion consumer Lambda, which starts the
 * Bedrock ingestion job through the tenant's `CcfDestinationRole`. Batch size 1 with partial batch
 * responses: the consumer reports a retryable message as failed and never fails the whole batch.
 * The job and connector deletion workflows request an ingestion through `requestIngestion`.
 */
export class IngestionQueue extends Construct {
  public readonly queue: Queue;
  public readonly deadLetterQueue: Queue;
  public readonly consumer: LambdaFunction;
  public readonly consumerRole: Role;
  private readonly tables: DatabaseTables;
  private readonly serviceKey: IKey;

  constructor(scope: Construct, id: string, props: IngestionQueueProps) {
    super(scope, id);
    const { tables, serviceKey, retention, removalPolicy, timeout, settings } = props;
    const {
      bundle,
      memorySize,
      logRetention,
      destinationWorkflowArn,
      connectorDeletionWorkflowArn,
    } = props;
    assertRetriesFitRetention({ settings, retention });
    this.tables = tables;
    this.serviceKey = serviceKey;
    const queueProps = { projectName: ProjectName.CCF, serviceKey, retention, removalPolicy };
    this.deadLetterQueue = new ServiceQueue(this, "DeadLetterQueue", {
      ...queueProps,
      name: ResourceName.INGESTION_DLQ,
    }).queue;
    this.queue = new ServiceQueue(this, "Queue", {
      ...queueProps,
      name: ResourceName.INGESTION_QUEUE,
      visibilityTimeout: Duration.seconds(timeout.toSeconds() * VISIBILITY_TIMEOUT_FACTOR),
      deadLetterQueue: { queue: this.deadLetterQueue, maxReceiveCount: settings.maxReceiveCount },
    }).queue;

    this.consumerRole = new Role(this, "ConsumerRole", {
      roleName: IngestionQueueName.CONSUMER_ROLE,
      path: ROLE_PATH,
      assumedBy: new ServicePrincipal("lambda.amazonaws.com"),
      managedPolicies: [
        ManagedPolicy.fromAwsManagedPolicyName("service-role/AWSLambdaBasicExecutionRole"),
      ],
      description: "Starts Bedrock ingestion through each tenant's CcfDestinationRole.",
    });
    tables.destinationsTable.grant(this.consumerRole, "dynamodb:GetItem", "dynamodb:UpdateItem");
    for (const statement of this.consumerStatements(props))
      this.consumerRole.addToPolicy(statement);

    this.consumer = new BackendFunction(this, "Consumer", {
      name: ResourceName.INGESTION_CONSUMER_LAMBDA,
      handler: "ingestion_consumer_handler.handler",
      bundle,
      role: this.consumerRole,
      environment: {
        ...handlerSettingsEnvironment(props),
        INGESTION_DEAD_LETTER_QUEUE_URL: this.deadLetterQueue.queueUrl,
        WORKFLOWS__DESTINATION: destinationWorkflowArn,
        WORKFLOWS__CONNECTOR_DELETION: connectorDeletionWorkflowArn,
      },
      memorySize,
      timeout,
      reservedConcurrentExecutions: settings.consumerReservedConcurrency,
      logRetention,
      logRemovalPolicy: removalPolicy,
    }).function;
    this.consumer.addEventSource(
      new SqsEventSource(this.queue, { batchSize: 1, reportBatchItemFailures: true }),
    );
    acknowledgeNagRules({
      scopes: [this.consumerRole],
      acknowledgments: [
        actionsWithoutResourceAcknowledgment([
          "cloudwatch:PutMetricData (held to the framework namespace)",
          ...XRAY_TRACE_ACTIONS,
        ]),
        assumeDestinationRolesAcknowledgment({
          scope: this,
          resources: destinationRoleArns(props),
        }),
      ],
    });
  }

  /**
   * The two states that ask for one ingestion of a `BEDROCK` destination: store a token unless one
   * waits, then send the waiting token. A destination that is gone or isn't `BEDROCK` goes to `next`.
   */
  public requestIngestion(scope: Construct, props: RequestIngestionProps): IChainable {
    const { statePrefix, tenantArnPrefix, destinationId, next } = props;
    const { partitionKey, sortKey } = tablesContract().destinations;
    const send = sdkCall(scope, `${statePrefix}SendIngestionMessage`, {
      service: SdkService.SQS,
      action: "sendMessage",
      iamResources: [this.queue.queueArn],
      arguments: {
        QueueUrl: this.queue.queueUrl,
        MessageBody:
          `{% $string({'tenant_arn_prefix': ${tenantArnPrefix}, 'destination_id': ${destinationId}, ` +
          "'token': $ingestionToken}) %}",
      },
    }).next(next);
    return sdkCall(scope, `${statePrefix}SetIngestionPending`, {
      service: SdkService.DYNAMODB,
      action: "updateItem",
      iamResources: [this.tables.destinationsTable.tableArn],
      arguments: {
        TableName: this.tables.destinationsTable.tableName,
        Key: {
          [partitionKey.name]: { S: `{% ${tenantArnPrefix} %}` },
          ...(sortKey === null ? {} : { [sortKey.name]: { S: `{% ${destinationId} %}` } }),
        },
        UpdateExpression: "SET ingestion_pending = if_not_exists(ingestion_pending, :token)",
        ConditionExpression: "attribute_exists(destination_id) AND #type = :bedrock",
        ExpressionAttributeNames: { "#type": "type" },
        ExpressionAttributeValues: {
          ":token": { S: "{% 'ing-' & $uuid() %}" },
          ":bedrock": { S: enumMember({ schema: "DestinationType", member: "BEDROCK" }) },
        },
        ReturnValues: "UPDATED_NEW",
      },
      assign: { ingestionToken: "{% $states.result.Attributes.ingestion_pending.S %}" },
      notFound: {
        errors: [
          sdkError({ service: SdkService.DYNAMODB, errorName: "ConditionalCheckFailedException" }),
        ],
        next,
      },
    }).next(send);
  }

  /** The service key, which encrypts the destinations table and the queue, for the workflow that requests. */
  public grantRequestIngestion(grantee: IGrantable): void {
    this.serviceKey.grant(grantee, "kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey");
  }

  /** The hand-written `CcfIngestionConsumerRole` statements; the event source grants the consume actions. */
  private consumerStatements(props: IngestionQueueProps): PolicyStatement[] {
    return [
      new PolicyStatement({
        sid: "AssumeDestinationRoles",
        effect: Effect.ALLOW,
        actions: ["sts:AssumeRole"],
        resources: destinationRoleArns(props),
      }),
      // grantSendMessages on a KMS queue also grants kms:ReEncrypt* and kms:GenerateDataKey*.
      new PolicyStatement({
        sid: "IngestionDeadLetterQueue",
        effect: Effect.ALLOW,
        actions: ["sqs:SendMessage"],
        resources: [this.deadLetterQueue.queueArn],
      }),
      new PolicyStatement({
        sid: "ServiceKey",
        effect: Effect.ALLOW,
        actions: ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey"],
        resources: [props.serviceKey.keyArn],
      }),
      new PolicyStatement({
        sid: "IngestionMetrics",
        effect: Effect.ALLOW,
        actions: ["cloudwatch:PutMetricData"],
        resources: ["*"],
        conditions: { StringEquals: { "cloudwatch:namespace": metricsContract().namespace } },
      }),
    ];
  }
}

interface RetryRetentionRequest {
  readonly settings: IngestionQueueSettings;
  readonly retention: Duration;
}

/** Every receive's delay, up to the dead-letter redrive, must fit in the time the queue keeps a message. */
const assertRetriesFitRetention = ({ settings, retention }: RetryRetentionRequest): void => {
  const { retryDelaysSeconds: delays, maxReceiveCount } = settings;
  const last = delays.at(-1);
  if (last === undefined) throw new Error(`${SETTINGS_KEY} must hold at least one delay.`);
  const total = Array.from(
    { length: maxReceiveCount },
    (_, receive) => delays[receive] ?? last,
  ).reduce((sum, delay) => sum + delay, 0);
  if (total > retention.toSeconds())
    throw new Error(
      `${SETTINGS_KEY} delays ${total} seconds over ${maxReceiveCount} receives, longer than the ` +
        `queue's ${retention.toSeconds()}-second retention.`,
    );
};
