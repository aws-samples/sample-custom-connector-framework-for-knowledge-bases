import { Aws, Duration, RemovalPolicy } from "aws-cdk-lib";
import { SpecRestApi } from "aws-cdk-lib/aws-apigateway";
import {
  Effect,
  ManagedPolicy,
  PolicyStatement,
  Role,
  ServicePrincipal,
} from "aws-cdk-lib/aws-iam";
import { IKey } from "aws-cdk-lib/aws-kms";
import { Function as LambdaFunction } from "aws-cdk-lib/aws-lambda";
import { RetentionDays } from "aws-cdk-lib/aws-logs";
import { IQueue } from "aws-cdk-lib/aws-sqs";
import { IStateMachine } from "aws-cdk-lib/aws-stepfunctions";
import { Construct } from "constructs";
import { ApiThrottlingConfig } from "../common/app-config";
import {
  ApiServiceName,
  JobExecutionName,
  LOG_ROUTER_LOGS,
  ProjectName,
  ROLE_PATH,
  ResourceName,
  SERVICE_SETTINGS,
  createResourceName,
} from "../common/config";
import { runtimeRoutes } from "../common/contracts";
import { destinationRoleArns } from "../common/destination-names";
import {
  acknowledgeNagRules,
  actionsWithoutResourceAcknowledgment,
  assumeDestinationRolesAcknowledgment,
  executionsFinding,
  resourceFindings,
  XRAY_TRACE_ACTIONS,
} from "../common/nag-suppressions";
import { API_STAGE_NAME, ApiDefinition } from "../constructs/api-definition";
import { BackendFunction } from "../constructs/backend-function";
import { BackendBundle } from "./backend-bundle";
import { DatabaseTables } from "./database-tables";
import { handlerSettingsEnvironment } from "./handler-settings";
import { JOB_DEFINITIONS, JobDispatch } from "./job-dispatch";
import { PASS_RUNTIME_ROLES_REASON, RuntimeRoleName } from "./runtime-identities";

/** API Gateway's integration limit, which the API and scheduled-start Lambdas' timeout matches. */
const API_INTEGRATION_TIMEOUT = Duration.seconds(29);

/** Every schedule in the framework's schedule group; the API creates one per custom connector. */
const schedulesArn = (schedulerGroupName: string): string =>
  `arn:${Aws.PARTITION}:scheduler:${Aws.REGION}:${Aws.ACCOUNT_ID}:schedule/${schedulerGroupName}/*`;

export interface ApiServiceProps {
  readonly tables: DatabaseTables;
  /** The service key; the API and scheduled-start Lambdas encrypt resource data with it. */
  readonly serviceKey: IKey;
  readonly serviceAccountId: string;
  readonly allowedTenantAccountIds: readonly string[];
  readonly throttling: ApiThrottlingConfig;
  /** The retention of the API access log group and both handler log groups. */
  readonly logRetention: RetentionDays;
  readonly removalPolicy: RemovalPolicy;
  readonly runtimeRoleBoundary: ManagedPolicy;
  readonly runtimeRoleArnPattern: string;
  readonly schedulerGroupName: string;
  readonly schedulerTargetRole: Role;
  readonly schedulerTargetDlq: IQueue;
  readonly destinationWorkflow: IStateMachine;
  /** `CcfDestinationDeletionWorkflow`, which the API never starts but whose executions it describes. */
  readonly destinationDeletionWorkflow: IStateMachine;
  readonly connectorDeletionWorkflow: IStateMachine;
  /** `CcfJobExecution`: the API starts, describes, and stops its executions and registers job definitions. */
  readonly jobDispatch: JobDispatch;
  /** `CcfDestinationWorkflow`'s ARN from its fixed name, the external ID of every `CcfDestinationRole` session. */
  readonly destinationWorkflowArn: string;
  readonly bundle: BackendBundle;
  readonly memorySize: number;
  readonly idempotencyExpirySeconds: number;
  readonly schedulerRetryAttempts: number;
  readonly schedulerMaxEventAgeSeconds: number;
  readonly jobs: ApiJobSettings;
}

/** The job values the API and scheduled-start Lambdas read. */
export interface ApiJobSettings {
  /** `CcfJobExecution`'s ARN, from its fixed name. */
  readonly executionArn: string;
  /** How long the jobs table keeps a finished job's record. */
  readonly recordRetention: Duration;
  /** How long it keeps a started job's record: the finished retention plus the longest job timeout. */
  readonly activeRecordRetention: Duration;
  /** The role every job definition names to pull the connector image and write the log router's logs. */
  readonly executionRoleArn: string;
  readonly logRouterImage: string;
}

/** The ARN a Lambda function with a fixed name will have, known before the function exists. */
const functionArnFor = (resourceName: ResourceName): string =>
  `arn:${Aws.PARTITION}:lambda:${Aws.REGION}:${Aws.ACCOUNT_ID}:function:${createResourceName({ projectName: ProjectName.CCF, resourceName })}`;

/**
 * The framework API, its Lambda (role `CcfApiRole`), and the scheduled-start Lambda that EventBridge
 * Scheduler invokes. The API's body names the API Lambda by its fixed function name, not by a
 * reference, so the Lambda can carry the REST API ID in its settings without a CloudFormation cycle.
 */
export class ApiService extends Construct {
  public readonly api: SpecRestApi;
  public readonly apiRole: Role;
  public readonly apiLambda: LambdaFunction;
  public readonly scheduledStartLambda: LambdaFunction;

  constructor(scope: Construct, id: string, props: ApiServiceProps) {
    super(scope, id);
    const { tables, serviceKey, serviceAccountId, allowedTenantAccountIds, schedulerTargetRole } =
      props;
    const { bundle, memorySize, logRetention, removalPolicy, idempotencyExpirySeconds } = props;
    this.api = new ApiDefinition(this, "Definition", {
      functionArn: functionArnFor(ResourceName.API_LAMBDA),
      throttling: props.throttling,
      invokerAccountIds: [serviceAccountId, ...allowedTenantAccountIds],
      logRetention,
      logRemovalPolicy: removalPolicy,
    }).api;
    this.apiRole = new Role(this, "ApiRole", {
      roleName: ApiServiceName.API_ROLE,
      path: ROLE_PATH,
      assumedBy: new ServicePrincipal("lambda.amazonaws.com"),
      managedPolicies: [
        ManagedPolicy.fromAwsManagedPolicyName("service-role/AWSLambdaBasicExecutionRole"),
      ],
      description:
        "The framework API Lambda's role; reaches tenant buckets only through CcfDestinationRole.",
    });
    const handlerSettings = handlerSettingsEnvironment({ tables, serviceKey, serviceAccountId });
    const { jobs } = props;
    const jobSettings = {
      JOBS__JOB_EXECUTION_ARN: jobs.executionArn,
      JOBS__RECORD_RETENTION_SECONDS: String(jobs.recordRetention.toSeconds()),
      JOBS__ACTIVE_RECORD_RETENTION_SECONDS: String(jobs.activeRecordRetention.toSeconds()),
    };
    const lambda = {
      bundle,
      memorySize,
      timeout: API_INTEGRATION_TIMEOUT,
      logRetention,
      logRemovalPolicy: removalPolicy,
    };
    this.apiLambda = new BackendFunction(this, ResourceName.API_LAMBDA, {
      ...lambda,
      name: ResourceName.API_LAMBDA,
      handler: "api_handler.handler",
      role: this.apiRole,
      environment: {
        ...handlerSettings,
        AWS_URL_SUFFIX: Aws.URL_SUFFIX,
        IDEMPOTENCY__EXPIRY_SECONDS: String(idempotencyExpirySeconds),
        RUNTIME_ROLES__REST_API_ID: this.api.restApiId,
        RUNTIME_ROLES__STAGE_NAME: API_STAGE_NAME,
        RUNTIME_ROLES__BOUNDARY_ARN: props.runtimeRoleBoundary.managedPolicyArn,
        RUNTIME_ROLES__ROUTES: JSON.stringify(runtimeRoutes()),
        RUNTIME_ROLES__ROLE_PATH: RuntimeRoleName.PATH,
        RUNTIME_ROLES__ROLE_NAME_PREFIX: RuntimeRoleName.NAME_PREFIX,
        RUNTIME_ROLES__POLICY_NAME: RuntimeRoleName.POLICY,
        RUNTIME_ROLES__MAX_SESSION_SECONDS: String(
          SERVICE_SETTINGS.runtimeRoleMaxSession.toSeconds(),
        ),
        SCHEDULER__TARGET_ARN: functionArnFor(ResourceName.SCHEDULED_START_LAMBDA),
        SCHEDULER__ROLE_ARN: schedulerTargetRole.roleArn,
        SCHEDULER__GROUP_NAME: props.schedulerGroupName,
        SCHEDULER__DEAD_LETTER_ARN: props.schedulerTargetDlq.queueArn,
        SCHEDULER__RETRY_ATTEMPTS: String(props.schedulerRetryAttempts),
        SCHEDULER__MAX_EVENT_AGE_SECONDS: String(props.schedulerMaxEventAgeSeconds),
        WORKFLOWS__DESTINATION: props.destinationWorkflowArn,
        WORKFLOWS__CONNECTOR_DELETION: props.connectorDeletionWorkflow.stateMachineArn,
        ...jobSettings,
        JOB_DEFINITIONS__NAME_PREFIX: JobExecutionName.DEFINITION_PREFIX,
        JOB_DEFINITIONS__EXECUTION_ROLE_ARN: jobs.executionRoleArn,
        // Built from the API's ID and stage name, not its URL, which depends on this Lambda's deployment.
        JOB_DEFINITIONS__API_ENDPOINT: `https://${this.api.restApiId}.execute-api.${Aws.REGION}.${Aws.URL_SUFFIX}/${API_STAGE_NAME}/`,
        JOB_DEFINITIONS__LOG_ROUTER_IMAGE: jobs.logRouterImage,
        JOB_DEFINITIONS__LOG_ROUTER_LOG_GROUP: LOG_ROUTER_LOGS.logGroup,
        JOB_DEFINITIONS__LOG_ROUTER_STREAM_PREFIX: LOG_ROUTER_LOGS.streamPrefix,
        ALLOWED_TENANT_ACCOUNT_IDS: allowedTenantAccountIds.join(","),
      },
    }).function;
    this.scheduledStartLambda = new BackendFunction(this, ResourceName.SCHEDULED_START_LAMBDA, {
      ...lambda,
      name: ResourceName.SCHEDULED_START_LAMBDA,
      handler: "scheduled_start_handler.handler",
      environment: {
        ...handlerSettings,
        IDEMPOTENCY__EXPIRY_SECONDS: String(idempotencyExpirySeconds),
        ...jobSettings,
      },
    }).function;
    this.apiLambda.addPermission("ApiGatewayInvokePermission", {
      principal: new ServicePrincipal("apigateway.amazonaws.com"),
      action: "lambda:InvokeFunction",
      sourceArn: this.api.arnForExecuteApi(),
    });
    // The API's body names the Lambda by name, so deploy the stage only once the Lambda exists.
    this.api.deploymentStage.node.addDependency(this.apiLambda);
    this.grantApi(props);
    this.grantScheduledStart(props);
    acknowledgeNagRules({
      scopes: [this.apiRole, this.scheduledStartLambda],
      acknowledgments: [
        actionsWithoutResourceAcknowledgment(XRAY_TRACE_ACTIONS),
        {
          id: "AwsSolutions-IAM5",
          reason:
            "Job executions are named by job ID, so DescribeExecution names CcfJobExecution's executions.",
          findings: [executionsFinding(props.jobDispatch.stateMachine)],
        },
      ],
    });
    acknowledgeNagRules({
      scopes: [this.apiRole],
      acknowledgments: [
        assumeDestinationRolesAcknowledgment({
          scope: this,
          resources: destinationRoleArns(props),
        }),
        {
          id: "AwsSolutions-IAM5",
          reason: PASS_RUNTIME_ROLES_REASON,
          findings: resourceFindings({ scope: this, resources: [props.runtimeRoleArnPattern] }),
        },
        {
          id: "AwsSolutions-IAM5",
          reason:
            "Custom connector schedules are created at run time, all in the one framework schedule group; " +
            "workflow executions are named per request, so DescribeExecution names each workflow's executions; " +
            `job definitions are registered per custom connector, named ${JobExecutionName.DEFINITION_PREFIX}*.`,
          findings: [
            ...[
              props.destinationWorkflow,
              props.destinationDeletionWorkflow,
              props.connectorDeletionWorkflow,
            ].map(executionsFinding),
            ...resourceFindings({
              scope: this,
              resources: [
                schedulesArn(props.schedulerGroupName),
                JOB_DEFINITIONS,
                `${JOB_DEFINITIONS}:*`,
              ],
            }),
          ],
        },
      ],
    });
  }

  /** `CcfApiRole`: its tables, the key, both workflows, the tenant roles, runtime roles, and schedules. */
  private grantApi(props: ApiServiceProps): void {
    const {
      tables,
      serviceKey,
      destinationWorkflow,
      destinationDeletionWorkflow,
      connectorDeletionWorkflow,
      runtimeRoleArnPattern,
    } = props;
    const { runtimeRoleBoundary, schedulerGroupName, schedulerTargetRole } = props;
    const role = this.apiRole;
    for (const table of tables.all) table.grantReadWriteData(role);
    serviceKey.grantEncryptDecrypt(role);
    for (const workflow of [destinationWorkflow, connectorDeletionWorkflow])
      workflow.grantStartExecution(role);
    // A destination's recorded execution may be a continued deletion, so the API describes both.
    for (const workflow of [
      destinationWorkflow,
      destinationDeletionWorkflow,
      connectorDeletionWorkflow,
    ])
      workflow.grantExecution(role, "states:DescribeExecution");
    const jobExecution = props.jobDispatch.stateMachine;
    jobExecution.grantStartExecution(role);
    jobExecution.grantExecution(role, "states:DescribeExecution", "states:StopExecution");
    props.jobDispatch.grantJobDefinitionRegistration(role);
    for (const statement of [
      new PolicyStatement({
        sid: "AssumeDestinationRoles",
        effect: Effect.ALLOW,
        actions: ["sts:AssumeRole"],
        resources: destinationRoleArns(props),
      }),
      new PolicyStatement({
        sid: "RuntimeRoleTags",
        effect: Effect.ALLOW,
        actions: ["iam:TagRole", "iam:UntagRole"],
        resources: [runtimeRoleArnPattern],
      }),
      new PolicyStatement({
        sid: "CreateRuntimeRoles",
        effect: Effect.ALLOW,
        actions: ["iam:CreateRole"],
        resources: [runtimeRoleArnPattern],
        conditions: {
          StringEquals: { "iam:PermissionsBoundary": runtimeRoleBoundary.managedPolicyArn },
        },
      }),
      new PolicyStatement({
        sid: "ManageRuntimeRoles",
        effect: Effect.ALLOW,
        actions: ["iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:DeleteRole"],
        resources: [runtimeRoleArnPattern],
      }),
      new PolicyStatement({
        sid: "Schedules",
        effect: Effect.ALLOW,
        actions: [
          "scheduler:CreateSchedule",
          "scheduler:UpdateSchedule",
          "scheduler:DeleteSchedule",
        ],
        resources: [schedulesArn(schedulerGroupName)],
      }),
      new PolicyStatement({
        sid: "PassSchedulerRole",
        effect: Effect.ALLOW,
        actions: ["iam:PassRole"],
        resources: [schedulerTargetRole.roleArn],
        conditions: { StringEquals: { "iam:PassedToService": "scheduler.amazonaws.com" } },
      }),
    ])
      role.addToPolicy(statement);
    schedulerTargetRole.addToPolicy(
      new PolicyStatement({
        effect: Effect.ALLOW,
        actions: ["lambda:InvokeFunction"],
        resources: [functionArnFor(ResourceName.SCHEDULED_START_LAMBDA)],
      }),
    );
  }

  private grantScheduledStart({ tables, serviceKey, jobDispatch }: ApiServiceProps): void {
    const handler = this.scheduledStartLambda;
    for (const table of [tables.connectorsTable, tables.jobsTable, tables.idempotencyTable])
      table.grantReadWriteData(handler);
    // The start transaction only checks that the destination is ACTIVE.
    tables.destinationsTable.grantReadData(handler);
    jobDispatch.stateMachine.grantStartExecution(handler);
    jobDispatch.stateMachine.grantExecution(handler, "states:DescribeExecution");
    serviceKey.grantEncryptDecrypt(this.scheduledStartLambda);
  }
}
