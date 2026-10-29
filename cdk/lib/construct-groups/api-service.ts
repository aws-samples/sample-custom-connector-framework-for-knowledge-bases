import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { CfnOutput, Duration, Stack } from "aws-cdk-lib";
import {
  ApiDefinition,
  LogGroupLogDestination,
  MethodLoggingLevel,
  SpecRestApi,
} from "aws-cdk-lib/aws-apigateway";
import { JobQueue } from "aws-cdk-lib/aws-batch";
import { Table } from "aws-cdk-lib/aws-dynamodb";
import { Rule } from "aws-cdk-lib/aws-events";
import { LambdaFunction } from "aws-cdk-lib/aws-events-targets";
import {
  AccountPrincipal,
  Effect,
  ManagedPolicy,
  PolicyDocument,
  PolicyStatement,
  Role,
  ServicePrincipal,
} from "aws-cdk-lib/aws-iam";
import { IKey } from "aws-cdk-lib/aws-kms";
import {
  Code,
  Function as LambdaFn,
  Runtime,
  StartingPosition,
  Tracing,
} from "aws-cdk-lib/aws-lambda";
import { DynamoEventSource, SqsDlq } from "aws-cdk-lib/aws-lambda-event-sources";
import { LogGroup, RetentionDays } from "aws-cdk-lib/aws-logs";
import { IQueue } from "aws-cdk-lib/aws-sqs";
import { Construct } from "constructs";
import {
  ProjectName,
  Provider,
  ResourceName,
  createConstructId,
  createResourceName,
  resolveAllowedClientAccountIds,
} from "../common/config";
import { DockerImageProps } from "./compute-image";

const API_INTEGRATION_TIMEOUT = Duration.seconds(29);
const LAMBDA_MEMORY_MB = 1024;
const JOB_NAME_PREFIX = "ccj-";
const BATCH_OWNERSHIP_TAG_KEY = "ccf:owned-by";
const BATCH_OWNERSHIP_TAG_VALUE = "custom-connector-framework";
const ASYNC_RETRY_ATTEMPTS = 3;
const ASYNC_MAX_EVENT_AGE = Duration.hours(1);
const LAMBDA_INTEGRATION_URI_PLACEHOLDER = "__CCF_API_LAMBDA_INTEGRATION_URI__";
const OPENAPI_PATH = resolve(__dirname, "../../../api/generated/openapi.json");
const RUNTIME_CALLABLE_EXTENSION = "x-ccf-runtime-callable";

type JsonObject = Record<string, unknown>;

const isJsonObject = (value: unknown): value is JsonObject =>
  typeof value === "object" && value !== null && !Array.isArray(value);

interface CreateApiRequest {
  readonly projectName: ProjectName;
  readonly region: string;
  readonly account: string;
  readonly allowedClientAccountIds: readonly string[];
}

interface CreateApiDefinitionRequest {
  readonly functionArn: string;
  readonly partition: string;
  readonly region: string;
}

interface GrantBatchAccessRequest {
  readonly region: string;
  readonly account: string;
  readonly jobQueue: JobQueue;
  readonly frameworkExecutionPrincipal: Role;
  readonly runtimeRoleArnPattern: string;
}

interface GrantRuntimeRoleLifecycleRequest {
  readonly runtimeRoleBoundary: ManagedPolicy;
  readonly runtimeRoleArnPattern: string;
}

/**
 * Read the connector-runtime route templates the Smithy model marks and the
 * API build writes into the generated OpenAPI. This is the only inventory of
 * runtime-callable routes; the control plane turns each template into an exact
 * per-connector execute-api ARN.
 */
const readRuntimeCallableRoutes = (): { readonly method: string; readonly path: string }[] => {
  let document: unknown;
  try {
    document = JSON.parse(readFileSync(OPENAPI_PATH, "utf8"));
  } catch (error: unknown) {
    throw new Error(
      `Unable to load generated OpenAPI at ${OPENAPI_PATH}. Run 'make -C api build' first.`,
      { cause: error },
    );
  }
  if (!isJsonObject(document) || !isJsonObject(document.paths)) {
    throw new Error(`Generated OpenAPI at ${OPENAPI_PATH} must contain paths.`);
  }
  const routes = Object.entries(document.paths).flatMap(([path, pathItem]) =>
    isJsonObject(pathItem)
      ? Object.entries(pathItem).flatMap(([method, operation]) =>
          isJsonObject(operation) && operation[RUNTIME_CALLABLE_EXTENSION] === true
            ? [{ method: method.toUpperCase(), path }]
            : [],
        )
      : [],
  );
  if (routes.length === 0) {
    throw new Error(
      `Generated OpenAPI at ${OPENAPI_PATH} marks no ${RUNTIME_CALLABLE_EXTENSION} operation.`,
    );
  }
  return routes.sort(
    (first, second) =>
      first.path.localeCompare(second.path) || first.method.localeCompare(second.method),
  );
};

const createApiDefinition = ({
  functionArn,
  partition,
  region,
}: CreateApiDefinitionRequest): JsonObject => {
  const integrationUri = `arn:${partition}:apigateway:${region}:lambda:path/2015-03-31/functions/${functionArn}/invocations`;
  let replacementCount = 0;
  let definition: unknown;
  try {
    definition = JSON.parse(
      readFileSync(OPENAPI_PATH, "utf8"),
      (key: string, value: unknown): unknown => {
        void key;
        if (value === LAMBDA_INTEGRATION_URI_PLACEHOLDER) {
          replacementCount += 1;
          return integrationUri;
        }
        return value;
      },
    );
  } catch (error: unknown) {
    throw new Error(
      `Unable to load generated OpenAPI at ${OPENAPI_PATH}. Run 'make -C api build' first.`,
      { cause: error },
    );
  }
  if (!isJsonObject(definition)) {
    throw new Error(`Generated OpenAPI at ${OPENAPI_PATH} must contain a JSON object.`);
  }
  if (replacementCount === 0) {
    throw new Error(
      `Generated OpenAPI at ${OPENAPI_PATH} contains no Lambda integration placeholders.`,
    );
  }
  return definition;
};

export interface ApiServiceProps {
  readonly projectName: ProjectName;
  readonly provider: Provider;
  readonly region: string;
  readonly account: string;
  readonly allowedClientAccountIds: readonly string[];
  readonly connectorsTable: Table;
  readonly jobsTable: Table;
  readonly documentsTable: Table;
  readonly configurationKey: IKey;
  readonly jobQueue: JobQueue;
  readonly ingestionBucketName: string;
  readonly frameworkExecutionPrincipal: Role;
  readonly runtimeRoleBoundary: ManagedPolicy;
  readonly runtimeRoleArnPattern: string;
  readonly schedulerGroupName: string;
  readonly schedulerTargetRole: Role;
  readonly schedulerTargetDlq: IQueue;
  readonly jobsStreamDlq: IQueue;
  readonly batchStatusDlq: IQueue;
  readonly bedrockKnowledgeBaseId?: string;
  readonly bedrockDataSourceId?: string;
  readonly image: DockerImageProps;
}

/** Framework control plane, shared schedule target, and event-driven job dispatch. */
export class ApiService extends Construct {
  public readonly api: SpecRestApi;
  public readonly apiLambda: LambdaFn;
  public readonly scheduledStartLambda: LambdaFn;
  public readonly jobOrchestratorLambda: LambdaFn;
  public readonly jobStatusLambda: LambdaFn;

  constructor(scope: Construct, id: string, props: ApiServiceProps) {
    super(scope, id);

    const {
      projectName,
      provider,
      region,
      account,
      allowedClientAccountIds,
      connectorsTable,
      jobsTable,
      documentsTable,
      configurationKey,
      jobQueue,
      ingestionBucketName,
      frameworkExecutionPrincipal,
      runtimeRoleBoundary,
      runtimeRoleArnPattern,
      schedulerGroupName,
      schedulerTargetRole,
      schedulerTargetDlq,
      jobsStreamDlq,
      batchStatusDlq,
      bedrockKnowledgeBaseId,
      bedrockDataSourceId,
      image,
    } = props;
    const destinationMode = provider.toUpperCase();
    const baseEnvironment: Record<string, string> = {
      CUSTOM_CONNECTORS_TABLE_NAME: connectorsTable.tableName,
      CUSTOM_CONNECTOR_JOBS_TABLE_NAME: jobsTable.tableName,
      CUSTOM_CONNECTOR_DOCUMENTS_TABLE_NAME: documentsTable.tableName,
      INGESTION_BUCKET_NAME: ingestionBucketName,
      DESTINATION_MODE: destinationMode,
      POWERTOOLS_SERVICE_NAME: "CustomConnectorFramework",
      POWERTOOLS_METRICS_NAMESPACE: "CustomConnectorFramework",
      LOG_LEVEL: "INFO",
    };
    const scheduledStartFunctionArn = `arn:${Stack.of(this).partition}:lambda:${region}:${account}:function:${createResourceName(ProjectName.CCF, ResourceName.SCHEDULED_START_LAMBDA)}`;
    const scheduleEnvironment = {
      CONFIGURATION_KEY_ARN: configurationKey.keyArn,
      RUNTIME_ROLE_BOUNDARY_ARN: runtimeRoleBoundary.managedPolicyArn,
      CONNECTOR_RUNTIME_ROUTES: JSON.stringify(readRuntimeCallableRoutes()),
      ALLOWED_CLIENT_ACCOUNT_IDS: resolveAllowedClientAccountIds({
        values: allowedClientAccountIds,
        frameworkAccount: account,
      }).join(","),
      SCHEDULER_TARGET_ARN: scheduledStartFunctionArn,
      SCHEDULER_ROLE_ARN: schedulerTargetRole.roleArn,
      SCHEDULER_GROUP_NAME: schedulerGroupName,
      SCHEDULER_DLQ_ARN: schedulerTargetDlq.queueArn,
    };

    this.apiLambda = this.createHandler(
      "ApiLambda",
      ResourceName.API_LAMBDA,
      "api_handler.handler",
      image,
      { ...baseEnvironment, ...scheduleEnvironment },
    );
    this.scheduledStartLambda = this.createHandler(
      "ScheduledStartLambda",
      ResourceName.SCHEDULED_START_LAMBDA,
      "scheduled_start_handler.handler",
      image,
      { ...baseEnvironment, ...scheduleEnvironment },
    );
    this.jobOrchestratorLambda = this.createHandler(
      "JobOrchestratorLambda",
      ResourceName.JOB_ORCHESTRATOR_LAMBDA,
      "job_orchestrator_handler.handler",
      image,
      {
        ...baseEnvironment,
        AWS_BATCH_JOB_QUEUE: jobQueue.jobQueueName,
        FRAMEWORK_EXECUTION_ROLE_ARN: frameworkExecutionPrincipal.roleArn,
      },
    );
    const jobStatusEnvironment: Record<string, string> = { ...baseEnvironment };
    if (provider === Provider.BEDROCK) {
      if (bedrockKnowledgeBaseId === undefined || bedrockDataSourceId === undefined) {
        throw new Error("Bedrock destination requires knowledge base and data source identifiers.");
      }
      jobStatusEnvironment.BEDROCK_KNOWLEDGE_BASE_ID = bedrockKnowledgeBaseId;
      jobStatusEnvironment.BEDROCK_DATA_SOURCE_ID = bedrockDataSourceId;
    }
    this.jobStatusLambda = this.createHandler(
      "JobStatusLambda",
      ResourceName.JOB_STATUS_LAMBDA,
      "job_status_handler.handler",
      image,
      jobStatusEnvironment,
    );

    this.grantTableAccess(connectorsTable, jobsTable, documentsTable);
    configurationKey.grantEncryptDecrypt(this.apiLambda);
    configurationKey.grantEncryptDecrypt(this.scheduledStartLambda);
    this.grantSchedulerAccess(region, account, schedulerGroupName, schedulerTargetRole);
    schedulerTargetRole.addToPolicy(
      new PolicyStatement({
        effect: Effect.ALLOW,
        actions: ["lambda:InvokeFunction"],
        resources: [scheduledStartFunctionArn],
      }),
    );
    this.wireJobStream(jobsTable, jobsStreamDlq);
    this.wireBatchStatusRule(batchStatusDlq);
    this.grantBatchAccess({
      region,
      account,
      jobQueue,
      frameworkExecutionPrincipal,
      runtimeRoleArnPattern,
    });
    this.grantRuntimeRoleLifecycle({ runtimeRoleBoundary, runtimeRoleArnPattern });

    // The API definition embeds this Lambda's ARN, so the REST API id cannot be
    // injected back into the API Lambda environment without a CloudFormation
    // cycle. The control plane reads it from the authenticated request context.
    this.api = this.createApi({ projectName, region, account, allowedClientAccountIds });
    this.jobOrchestratorLambda.addEnvironment("CUSTOM_CONNECTOR_API_ENDPOINT", this.api.url);
    if (provider === Provider.BEDROCK) {
      this.grantBedrockIngestion(account, region, bedrockKnowledgeBaseId as string);
    }

    new CfnOutput(this, "ApiEndpoint", {
      value: this.api.url,
      description: "Invoke URL of the Custom Connector Framework API.",
    });
    new CfnOutput(this, "IngestionBucketNameOutput", {
      value: ingestionBucketName,
      description: "S3 bucket connectors write documents and provider sidecars into.",
    });
  }

  private createHandler(
    constructId: string,
    resourceName: ResourceName,
    handler: string,
    image: DockerImageProps,
    environment: Record<string, string>,
  ): LambdaFn {
    const logGroup = new LogGroup(this, `${constructId}Logs`, {
      retention: RetentionDays.ONE_MONTH,
      removalPolicy: image.logRemovalPolicy,
    });
    const handlerFunction = new LambdaFn(this, constructId, {
      functionName: createResourceName(ProjectName.CCF, resourceName),
      runtime: Runtime.PYTHON_3_14,
      handler,
      code: Code.fromAsset(image.sourcePath, { bundling: image.bundling }),
      environment,
      tracing: Tracing.ACTIVE,
      timeout: API_INTEGRATION_TIMEOUT,
      memorySize: LAMBDA_MEMORY_MB,
      logGroup,
    });
    return handlerFunction;
  }

  private grantTableAccess(connectorsTable: Table, jobsTable: Table, documentsTable: Table): void {
    for (const table of [connectorsTable, jobsTable, documentsTable]) {
      table.grantReadWriteData(this.apiLambda);
    }
    for (const handler of [this.scheduledStartLambda, this.jobOrchestratorLambda]) {
      connectorsTable.grantReadWriteData(handler);
      jobsTable.grantReadWriteData(handler);
    }
    jobsTable.grantStreamRead(this.jobOrchestratorLambda);
    connectorsTable.grantReadWriteData(this.jobStatusLambda);
    jobsTable.grantReadWriteData(this.jobStatusLambda);
  }

  private grantSchedulerAccess(
    region: string,
    account: string,
    schedulerGroupName: string,
    schedulerTargetRole: Role,
  ): void {
    const apiRole = this.apiLambda.role;
    if (apiRole === undefined) {
      throw new Error("API Lambda must have an execution role.");
    }
    apiRole.addToPrincipalPolicy(
      new PolicyStatement({
        effect: Effect.ALLOW,
        actions: [
          "scheduler:CreateSchedule",
          "scheduler:UpdateSchedule",
          "scheduler:DeleteSchedule",
          "scheduler:GetSchedule",
        ],
        resources: [
          `arn:${Stack.of(this).partition}:scheduler:${region}:${account}:schedule/${schedulerGroupName}/*`,
        ],
      }),
    );
    apiRole.addToPrincipalPolicy(
      new PolicyStatement({
        effect: Effect.ALLOW,
        actions: ["iam:PassRole"],
        resources: [schedulerTargetRole.roleArn],
        conditions: { StringEquals: { "iam:PassedToService": "scheduler.amazonaws.com" } },
      }),
    );
  }

  private wireJobStream(jobsTable: Table, jobsStreamDlq: IQueue): void {
    this.jobOrchestratorLambda.addEventSource(
      new DynamoEventSource(jobsTable, {
        startingPosition: StartingPosition.LATEST,
        batchSize: 1,
        bisectBatchOnError: true,
        retryAttempts: ASYNC_RETRY_ATTEMPTS,
        onFailure: new SqsDlq(jobsStreamDlq),
        filters: [
          {
            pattern: JSON.stringify({
              eventName: ["INSERT", "MODIFY"],
              dynamodb: { NewImage: { status: { S: ["STARTED", "STOPPING"] } } },
            }),
          },
        ],
      }),
    );
  }

  private wireBatchStatusRule(batchStatusDlq: IQueue): void {
    const rule = new Rule(this, createConstructId(ResourceName.BATCH_JOB_STATUS_RULE), {
      eventPattern: {
        source: ["aws.batch"],
        detailType: ["Batch Job State Change"],
        detail: { status: ["SUCCEEDED", "FAILED"], jobName: [{ prefix: JOB_NAME_PREFIX }] },
      },
    });
    rule.addTarget(
      new LambdaFunction(this.jobStatusLambda, {
        deadLetterQueue: batchStatusDlq,
        maxEventAge: ASYNC_MAX_EVENT_AGE,
        retryAttempts: ASYNC_RETRY_ATTEMPTS,
      }),
    );
  }

  private grantBatchAccess({
    region,
    account,
    jobQueue,
    frameworkExecutionPrincipal,
    runtimeRoleArnPattern,
  }: GrantBatchAccessRequest): void {
    const role = this.jobOrchestratorLambda.role;
    if (role === undefined) {
      throw new Error("Job orchestrator Lambda must have an execution role.");
    }
    const partition = Stack.of(this).partition;
    const jobArn = `arn:${partition}:batch:${region}:${account}:job/*`;
    const jobDefinitionArn = `arn:${partition}:batch:${region}:${account}:job-definition/${JOB_NAME_PREFIX}*`;
    const jobDefinitionRevisionArn = `${jobDefinitionArn}:*`;
    for (const handler of [this.jobOrchestratorLambda, this.jobStatusLambda]) {
      handler.addToRolePolicy(
        new PolicyStatement({
          effect: Effect.ALLOW,
          actions: ["batch:DescribeJobDefinitions"],
          // AWS Batch does not support resource-level permissions for DescribeJobDefinitions.
          resources: ["*"],
        }),
      );
      handler.addToRolePolicy(
        new PolicyStatement({
          effect: Effect.ALLOW,
          actions: ["batch:DeregisterJobDefinition"],
          resources: [jobDefinitionRevisionArn],
        }),
      );
    }
    role.addToPrincipalPolicy(
      new PolicyStatement({
        effect: Effect.ALLOW,
        actions: ["batch:RegisterJobDefinition", "batch:TagResource"],
        // One definition is registered per service-generated ccj-* job id. The
        // revision is unknown until registration, so the suffix wildcard is required.
        resources: [jobDefinitionArn],
      }),
    );
    role.addToPrincipalPolicy(
      new PolicyStatement({
        effect: Effect.ALLOW,
        actions: ["batch:SubmitJob"],
        resources: [jobQueue.jobQueueArn, jobDefinitionArn, jobArn],
        conditions: {
          StringEquals: {
            [`aws:RequestTag/${BATCH_OWNERSHIP_TAG_KEY}`]: BATCH_OWNERSHIP_TAG_VALUE,
          },
        },
      }),
    );
    role.addToPrincipalPolicy(
      new PolicyStatement({
        effect: Effect.ALLOW,
        actions: ["batch:TerminateJob"],
        resources: [jobArn],
        conditions: {
          StringEquals: {
            [`aws:ResourceTag/${BATCH_OWNERSHIP_TAG_KEY}`]: BATCH_OWNERSHIP_TAG_VALUE,
          },
        },
      }),
    );
    role.addToPrincipalPolicy(
      new PolicyStatement({
        effect: Effect.ALLOW,
        // Replay proves ownership of a possibly submitted execution from the
        // persisted queue, job name, and job-definition revision.
        actions: ["batch:ListJobs"],
        resources: [jobQueue.jobQueueArn],
      }),
    );
    role.addToPrincipalPolicy(
      new PolicyStatement({
        effect: Effect.ALLOW,
        actions: ["iam:PassRole"],
        resources: [frameworkExecutionPrincipal.roleArn, runtimeRoleArnPattern],
        conditions: { StringEquals: { "iam:PassedToService": "ecs-tasks.amazonaws.com" } },
      }),
    );
  }

  private grantRuntimeRoleLifecycle({
    runtimeRoleBoundary,
    runtimeRoleArnPattern,
  }: GrantRuntimeRoleLifecycleRequest): void {
    const apiRole = this.apiLambda.role;
    if (apiRole === undefined) {
      throw new Error("API Lambda must have an execution role.");
    }
    apiRole.addToPrincipalPolicy(
      new PolicyStatement({
        effect: Effect.ALLOW,
        actions: ["iam:CreateRole"],
        // Every connector runtime role is created under one service-owned path
        // and can never exceed the boundary this condition requires.
        resources: [runtimeRoleArnPattern],
        conditions: {
          StringEquals: { "iam:PermissionsBoundary": runtimeRoleBoundary.managedPolicyArn },
        },
      }),
    );
    apiRole.addToPrincipalPolicy(
      new PolicyStatement({
        effect: Effect.ALLOW,
        actions: ["iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:DeleteRole"],
        resources: [runtimeRoleArnPattern],
      }),
    );
  }

  private grantBedrockIngestion(account: string, region: string, knowledgeBaseId: string): void {
    this.jobStatusLambda.addToRolePolicy(
      new PolicyStatement({
        effect: Effect.ALLOW,
        actions: ["bedrock:StartIngestionJob"],
        // StartIngestionJob supports only the knowledge-base IAM resource type;
        // the exact data source id is supplied in the validated API request.
        resources: [
          `arn:${Stack.of(this).partition}:bedrock:${region}:${account}:knowledge-base/${knowledgeBaseId}`,
        ],
      }),
    );
  }

  private createApi({
    projectName,
    region,
    account,
    allowedClientAccountIds,
  }: CreateApiRequest): SpecRestApi {
    const validatedClientAccountIds = resolveAllowedClientAccountIds({
      values: allowedClientAccountIds,
      frameworkAccount: account,
    });
    const accessLogGroup = new LogGroup(this, "ApiAccessLogs", {
      retention: RetentionDays.ONE_MONTH,
    });
    const apiDefinition = createApiDefinition({
      functionArn: this.apiLambda.functionArn,
      partition: Stack.of(this).partition,
      region,
    });
    const api = new SpecRestApi(this, createConstructId(ResourceName.API), {
      apiDefinition: ApiDefinition.fromInline(apiDefinition),
      restApiName: createResourceName(projectName, ResourceName.API),
      description: "Custom Connector Framework control-plane API (SigV4/IAM authorized).",
      deployOptions: {
        accessLogDestination: new LogGroupLogDestination(accessLogGroup),
        loggingLevel: MethodLoggingLevel.INFO,
        metricsEnabled: true,
        tracingEnabled: true,
      },
      policy: new PolicyDocument({
        statements: [
          new PolicyStatement({
            effect: Effect.ALLOW,
            principals: [account, ...validatedClientAccountIds].map(
              (allowedAccountId) => new AccountPrincipal(allowedAccountId),
            ),
            actions: ["execute-api:Invoke"],
            // The API id is unresolved here. The explicit account principals and
            // control-plane path scope prevent public or unconfigured-account access.
            resources: [
              `arn:${Stack.of(this).partition}:execute-api:${region}:${account}:*/*/*/api/v1/custom-connectors*`,
            ],
          }),
        ],
      }),
    });
    this.apiLambda.addPermission("ApiGatewayInvokePermission", {
      principal: new ServicePrincipal("apigateway.amazonaws.com"),
      action: "lambda:InvokeFunction",
      sourceArn: api.arnForExecuteApi(),
    });
    return api;
  }
}
