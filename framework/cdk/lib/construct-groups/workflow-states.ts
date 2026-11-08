import { Duration, RemovalPolicy } from "aws-cdk-lib";
import { IRole } from "aws-cdk-lib/aws-iam";
import { LogGroup, RetentionDays } from "aws-cdk-lib/aws-logs";
import {
  Choice,
  Condition,
  CustomState,
  DefinitionBody,
  Fail,
  IChainable,
  IStateMachine,
  IntegrationPattern,
  JitterType,
  LogLevel,
  Pass,
  QueryLanguage,
  StateMachine,
  StateMachineType,
  TaskInput,
  TaskRole,
  TaskStateBase,
  Timeout,
} from "aws-cdk-lib/aws-stepfunctions";
import { CallAwsService, StepFunctionsStartExecution } from "aws-cdk-lib/aws-stepfunctions-tasks";
import { Construct } from "constructs";

/**
 * The JSONata builders every framework workflow uses: AWS SDK calls with the one retry policy, calls
 * run as a tenant role, continue-as-new, a `.sync` child execution, the object-deletion loop, and the
 * state machine itself. The CDK graph refuses a repeated state ID and renders only reachable states.
 */

/** The SDK integrations the workflows call, by Task resource service name. */
export enum SdkService {
  S3 = "s3",
  IAM = "iam",
  BEDROCK_AGENT = "bedrockagent",
  DYNAMODB = "dynamodb",
  CLOUDWATCH = "cloudwatch",
  SCHEDULER = "scheduler",
  SQS = "sqs",
  SFN = "sfn",
  BATCH = "batch",
  LOGS = "cloudwatchlogs",
}

/** Each service's exception prefix, and its throttling and server errors, from the Step Functions SDK integration list. */
const SERVICE_ERRORS: Readonly<
  Record<SdkService, { readonly prefix: string; readonly retried: readonly string[] }>
> = {
  [SdkService.S3]: { prefix: "S3", retried: ["SlowDownException", "InternalErrorException"] },
  [SdkService.IAM]: { prefix: "Iam", retried: ["ServiceFailureException"] },
  [SdkService.BEDROCK_AGENT]: { prefix: "BedrockAgent", retried: ["InternalServerException"] },
  [SdkService.DYNAMODB]: {
    prefix: "DynamoDb",
    retried: ["ProvisionedThroughputExceededException", "RequestLimitExceededException"],
  },
  [SdkService.CLOUDWATCH]: { prefix: "CloudWatch", retried: ["InternalServiceFaultException"] },
  [SdkService.SCHEDULER]: { prefix: "Scheduler", retried: ["InternalServerException"] },
  [SdkService.SQS]: { prefix: "Sqs", retried: ["RequestThrottledException"] },
  [SdkService.SFN]: { prefix: "Sfn", retried: [] },
  [SdkService.BATCH]: { prefix: "Batch", retried: ["ServerException"] },
  [SdkService.LOGS]: { prefix: "CloudWatchLogs", retried: [] },
};
const COMMON_RETRIED = [
  "ThrottlingException",
  "ServiceUnavailableException",
  "InternalServerErrorException",
];
export const TASK_TIMEOUT_ERROR = "States.Timeout";

/** 3 attempts, 2 seconds, doubling, with full jitter. */
export const RETRY_POLICY = {
  interval: Duration.seconds(2),
  maxAttempts: 3,
  backoffRate: 2,
  jitterStrategy: JitterType.FULL,
} as const;

/** The full error name of one service error, such as `S3.NoSuchBucketException`. */
export const sdkError = (request: {
  readonly service: SdkService;
  readonly errorName: string;
}): string => `${SERVICE_ERRORS[request.service].prefix}.${request.errorName}`;

const retriedErrors = (service: SdkService): string[] => [
  ...[...COMMON_RETRIED, ...SERVICE_ERRORS[service].retried].map((errorName) =>
    sdkError({ service, errorName }),
  ),
  TASK_TIMEOUT_ERROR,
];

/** A retrier for errors outside the policy, doubling with full jitter. */
export interface AdditionalRetry {
  readonly errors: readonly string[];
  readonly interval: Duration;
  readonly maxAttempts: number;
}

/** A tenant role the call runs as. */
export interface TenantCredentials {
  /** JSONata expression of the role ARN, such as `{% $destination.role_arn %}`. */
  readonly roleArn: string;
  /** The ARN patterns the workflow role may assume; the only resources it is granted for the call. */
  readonly roleArns: readonly string[];
}

/** A role ARN known only at run time, granted by pattern rather than by `*`. */
class TenantTaskRole extends TaskRole {
  constructor(
    public readonly roleArn: string,
    public readonly resource: string,
  ) {
    super();
  }
}

export interface SdkCallProps {
  readonly service: SdkService;
  /** camelCase, as CallAwsService takes it. */
  readonly action: string;
  /** Ignored when `tenant` is set: the workflow role is granted only `sts:AssumeRole` on the tenant roles. */
  readonly iamResources: readonly string[];
  /** Values may be `{% ... %}` expressions. */
  readonly arguments: Readonly<Record<string, unknown>>;
  /** JSONata; defaults to the state input. */
  readonly outputs?: string;
  readonly assign?: Readonly<Record<string, unknown>>;
  readonly tenant?: TenantCredentials;
  readonly additionalRetry?: AdditionalRetry;
  /** The only catch it adds: these errors go to `next`, with the state input as the output. */
  readonly notFound?: { readonly errors: readonly string[]; readonly next: IChainable };
}

const addAdditionalRetry = <T extends TaskStateBase | CustomState>(
  task: T,
  additional?: AdditionalRetry,
): T => {
  if (additional !== undefined)
    task.addRetry({
      errors: [...additional.errors],
      interval: additional.interval,
      maxAttempts: additional.maxAttempts,
      backoffRate: RETRY_POLICY.backoffRate,
      jitterStrategy: RETRY_POLICY.jitterStrategy,
    });
  return task;
};

/**
 * CallAwsService.jsonata with the one retry policy: the service's throttling and server errors and
 * `States.Timeout`. With `tenant`, `Credentials.RoleArn` is the expression and the workflow role is
 * granted `sts:AssumeRole` on `tenant.roleArns` only, never the called action and never `*`.
 */
export const sdkCall = (scope: Construct, id: string, props: SdkCallProps): CallAwsService => {
  const { service, action, tenant } = props;
  const firstRole = tenant?.roleArns.at(0);
  const call = CallAwsService.jsonata(scope, id, {
    service,
    action,
    parameters: { ...props.arguments },
    outputs: props.outputs ?? "{% $states.input %}",
    assign: props.assign === undefined ? undefined : { ...props.assign },
    iamAction: tenant === undefined ? undefined : "sts:AssumeRole",
    iamResources: tenant === undefined ? [...props.iamResources] : [...tenant.roleArns],
    credentials:
      tenant === undefined || firstRole === undefined
        ? undefined
        : { role: new TenantTaskRole(tenant.roleArn, firstRole) },
  });
  call.addRetry({ errors: retriedErrors(service), ...RETRY_POLICY });
  addAdditionalRetry(call, props.additionalRetry);
  if (props.notFound !== undefined)
    call.addCatch(props.notFound.next, {
      errors: [...props.notFound.errors],
      outputs: "{% $states.input %}",
    });
  return call;
};

/** The same retry policy, on `States.Timeout`, for any other task state, plus `additional`. */
export const withRetry = <T extends TaskStateBase | CustomState>(
  task: T,
  additional?: AdditionalRetry,
): T => {
  task.addRetry({ errors: [TASK_TIMEOUT_ERROR], ...RETRY_POLICY });
  return addAdditionalRetry(task, additional);
};

/** `{% $states.context.Execution.Input.<field> %}`, the one way a state reads the execution input. */
export const executionInput = (field: string): string =>
  `{% $states.context.Execution.Input.${field} %}`;

/** The most executions one deletion continues into, which stops a deletion that makes no progress. */
export const MAX_CONTINUATIONS = 1_000;

/**
 * `sfn:startExecution` of this state machine with the execution input and `continuation` one higher.
 * `stateMachineArn` is the machine's fixed-name ARN, granted without a reference to the machine itself.
 */
export const continueAsNew = (
  scope: Construct,
  id: string,
  props: { readonly executionName: string; readonly stateMachineArn: string },
): CallAwsService =>
  sdkCall(scope, id, {
    service: SdkService.SFN,
    action: "startExecution",
    iamResources: [props.stateMachineArn],
    arguments: {
      StateMachineArn: "{% $states.context.StateMachine.Id %}",
      Name: props.executionName,
      Input:
        "{% $string($merge([$states.context.Execution.Input, " +
        "{'continuation': $states.context.Execution.Input.continuation + 1}])) %}",
    },
  });

/** True once the execution has continued as often as it may. */
export const continuationsSpent = (): Condition =>
  Condition.jsonata(`{% $states.context.Execution.Input.continuation >= ${MAX_CONTINUATIONS} %}`);

/**
 * StepFunctionsStartExecution.jsonata, waiting for the child (`.sync`), associated with the parent.
 * With a `timeout`, a timed-out child isn't started again: the parent's catch handles it.
 */
export const childExecution = (
  scope: Construct,
  id: string,
  props: {
    readonly stateMachine: IStateMachine;
    /** The child's input members; each value may be a `{% ... %}` expression. */
    readonly input: Readonly<Record<string, unknown>>;
    readonly executionName: string;
    readonly timeout?: Duration;
  },
): StepFunctionsStartExecution => {
  const task = StepFunctionsStartExecution.jsonata(scope, id, {
    stateMachine: props.stateMachine,
    integrationPattern: IntegrationPattern.RUN_JOB,
    associateWithParent: true,
    input: TaskInput.fromObject({ ...props.input }),
    name: props.executionName,
    taskTimeout: props.timeout === undefined ? undefined : Timeout.duration(props.timeout),
  });
  return props.timeout === undefined ? withRetry(task) : task;
};

/**
 * The most keys one `ListObjectsV2` page returns to a workflow: 32 pages of the longest key in its
 * longest JSON form, with the page and state overhead, stay under the 256 KiB state limit (deploy fix 7).
 */
export const OBJECT_KEYS_PER_PAGE = 32;
/**
 * The loop passes one execution runs before it continues as new: a pass adds at most 32 history
 * events, so 600 passes stay under the 25,000-event limit with room for the other states (deploy fix 8).
 */
export const MAX_PASSES_PER_EXECUTION = 600;
/** Each loop state's `Assign` adds one pass. */
export const ADD_PASS = "{% $passes + 1 %}";

/** Starts the execution's loop pass count at 0 in `$passes`. */
export const startPassCount = (scope: Construct, id: string): Pass =>
  Pass.jsonata(scope, id, { assign: { passes: 0 } });

/** True once the execution has used its passes. */
export const passBudgetSpent = (): Condition =>
  Condition.jsonata(`{% $passes >= ${MAX_PASSES_PER_EXECUTION} %}`);

/** The `Error` of a refused object deletion. */
export const OBJECTS_NOT_DELETED = "ObjectsNotDeleted";

export interface ObjectDeletionLoopProps {
  /** Names the five states: List<name>Objects, Has<name>Objects, Delete<name>Objects, and the two checks. */
  readonly name: string;
  /** JSONata expression of the bucket name. */
  readonly bucketName: string;
  /** JSONata expression of the key prefix, when the loop deletes one prefix only. */
  readonly prefix?: string;
  readonly tenant: TenantCredentials;
  readonly done: IChainable;
  readonly budgetSpent: IChainable;
  readonly onMissingBucket: IChainable;
}

/**
 * The one object-deletion loop: list a page of keys, keeping only `KeyCount` and `Key`, delete them,
 * and list again until none are left, the bucket is gone, or the execution's passes are spent. S3
 * reports a refused key in `Errors` with HTTP 200, and a refused key would be listed again, so a
 * refusal fails the execution with the count and the first error code, never a key.
 */
export const objectDeletionLoop = (
  scope: Construct,
  props: ObjectDeletionLoopProps,
): IChainable => {
  const { name, bucketName, tenant } = props;
  const list = sdkCall(scope, `List${name}Objects`, {
    service: SdkService.S3,
    action: "listObjectsV2",
    iamResources: [],
    tenant,
    arguments: {
      Bucket: bucketName,
      MaxKeys: OBJECT_KEYS_PER_PAGE,
      FetchOwner: false,
      ...(props.prefix === undefined ? {} : { Prefix: props.prefix }),
    },
    outputs:
      "{% {'KeyCount': $states.result.KeyCount, 'Objects': [$states.result.Contents.{'Key': Key}]} %}",
    assign: { passes: ADD_PASS },
    notFound: {
      errors: [sdkError({ service: SdkService.S3, errorName: "NoSuchBucketException" })],
      next: props.onMissingBucket,
    },
  });
  const remove = sdkCall(scope, `Delete${name}Objects`, {
    service: SdkService.S3,
    action: "deleteObjects",
    iamResources: [],
    tenant,
    arguments: {
      Bucket: bucketName,
      Delete: { Objects: "{% $states.input.Objects %}", Quiet: true },
    },
    outputs:
      "{% {'ErrorCount': $count($states.result.Errors), " +
      "'ErrorCode': $states.result.Errors[0].Code ? $states.result.Errors[0].Code : null} %}",
    assign: { passes: ADD_PASS },
  });
  const refused = Fail.jsonata(scope, `${name}ObjectsNotDeleted`, {
    error: OBJECTS_NOT_DELETED,
    cause:
      "{% 'S3 refused to delete ' & $states.input.ErrorCount & ' objects; the first error code is ' & $states.input.ErrorCode & '.' %}",
  });
  const checked = Choice.jsonata(scope, `Check${name}ObjectsDeleted`)
    .when(Condition.jsonata("{% $states.input.ErrorCount > 0 %}"), refused)
    .otherwise(list);
  remove.next(checked);
  return list.next(
    Choice.jsonata(scope, `Has${name}Objects`)
      .when(Condition.jsonata("{% $states.input.KeyCount = 0 %}"), props.done)
      .when(passBudgetSpent(), props.budgetSpent)
      .otherwise(remove),
  );
};

export interface JsonataStateMachineProps {
  readonly stateMachineName: string;
  readonly definition: IChainable;
  readonly role: IRole;
  readonly logRetention: RetentionDays;
  readonly removalPolicy: RemovalPolicy;
}

/**
 * A Standard state machine in JSONata, with tracing, the removal policy, and `ERROR` logging with
 * execution data: every failure's log event carries its input's correlation ID, and logging every
 * transition would store each input again.
 */
export const jsonataStateMachine = (
  scope: Construct,
  id: string,
  props: JsonataStateMachineProps,
): StateMachine =>
  new StateMachine(scope, id, {
    stateMachineName: props.stateMachineName,
    stateMachineType: StateMachineType.STANDARD,
    queryLanguage: QueryLanguage.JSONATA,
    definitionBody: DefinitionBody.fromChainable(props.definition),
    role: props.role,
    tracingEnabled: true,
    removalPolicy: props.removalPolicy,
    logs: {
      destination: new LogGroup(scope, `${id}Logs`, {
        logGroupName: `/aws/vendedlogs/states/${props.stateMachineName}`,
        retention: props.logRetention,
        removalPolicy: props.removalPolicy,
      }),
      level: LogLevel.ERROR,
      includeExecutionData: true,
    },
  });
