import { Aws, CfnOutput, Duration, Stack, StackProps } from "aws-cdk-lib";
import { PermissionsBoundary } from "aws-cdk-lib/aws-iam";
import { Construct } from "constructs";
import * as path from "path";
import { FrameworkAppConfig } from "../common/app-config";
import { ConnectorDeletionWorkflowName, ProjectName, SERVICE_SETTINGS } from "../common/config";
import { numberRange } from "../common/contracts";
import { DestinationWorkflowName } from "../common/destination-names";
import { FrameworkExport } from "../common/framework-exports";
import { acknowledgeNagRules, resourceFindings } from "../common/nag-suppressions";
import { ApiService } from "../construct-groups/api-service";
import { createBackendBundle } from "../construct-groups/backend-bundle";
import { BatchCompute } from "../construct-groups/batch-compute";
import { ConnectorDeletionWorkflow } from "../construct-groups/connector-deletion-workflow";
import { DatabaseTables } from "../construct-groups/database-tables";
import { DeliveryResources } from "../construct-groups/delivery-resources";
import { DestinationWorkflows } from "../construct-groups/destination-workflows";
import { IngestionQueue } from "../construct-groups/ingestion-queue";
import { JobDispatch } from "../construct-groups/job-dispatch";
import { RuntimeIdentities } from "../construct-groups/runtime-identities";
import { ServiceKey } from "../constructs/service-key";
import { ServiceRoleBoundary } from "../constructs/service-role-boundary";

/** The deployer settings the service stack reads; the rest of `FrameworkAppConfig` is the app's. */
type ServiceStackConfig = Omit<FrameworkAppConfig, "costCenter" | "alarms">;

export interface CustomConnectorFrameworkStackProps extends StackProps, ServiceStackConfig {
  readonly projectName: ProjectName;
  /** The service Region, known at synth (the destination workflow's `CreateBucket` needs it). */
  readonly region: string;
}

interface FrameworkOutput {
  readonly id: string;
  readonly exportName: FrameworkExport;
  readonly value: string;
  readonly description: string;
}

/** The ARN a state machine with a fixed name will have; used where a reference would make a CloudFormation cycle. */
const stateMachineArnFor = (name: string): string =>
  `arn:${Aws.PARTITION}:states:${Aws.REGION}:${Aws.ACCOUNT_ID}:stateMachine:${name}`;

/**
 * `CustomConnectorFramework`: the service key, the six tables, the API and
 * its Lambdas, Batch compute, the runtime identities, the scheduler group, the two workflows,
 * and the ingestion queue. It holds no bucket, no knowledge base, and no image repository:
 * those live in tenant accounts. Every value another stack needs leaves as an export named in
 * {@link FrameworkExport}; the alarms live in `CustomConnectorFrameworkOps`.
 */
export class CustomConnectorFrameworkStack extends Stack {
  public readonly api: ApiService;
  public readonly jobDispatch: JobDispatch;
  public readonly destinationWorkflows: DestinationWorkflows;
  public readonly connectorDeletionWorkflow: ConnectorDeletionWorkflow;

  constructor(scope: Construct, id: string, props: CustomConnectorFrameworkStackProps) {
    super(scope, id, props);
    const { projectName, removalPolicy, logRetention, allowedTenantAccountIds } = props;
    // Every role in this stack, including the ones CDK adds, gets the service boundary.
    PermissionsBoundary.of(this).apply(new ServiceRoleBoundary(this, "ServiceRoleBoundary").policy);
    const serviceAccountId = Aws.ACCOUNT_ID;
    const accounts = { serviceAccountId, allowedTenantAccountIds };
    const serviceKey = new ServiceKey(this, "Encryption", { projectName, removalPolicy }).key;
    const tables = new DatabaseTables(this, "Database", { removalPolicy, serviceKey });
    acknowledgeNagRules({
      scopes: [this],
      acknowledgments: [
        {
          id: "AwsSolutions-IAM5",
          reason: "A CDK table grant covers the table's indexes.",
          findings: resourceFindings({ scope: this, resources: tables.indexesArns }),
        },
      ],
    });
    const queueRetention = SERVICE_SETTINGS.deadLetterRetention;
    const delivery = new DeliveryResources(this, "Delivery", {
      projectName,
      serviceKey,
      retention: queueRetention,
      removalPolicy,
    });
    const batch = new BatchCompute(this, "BatchCompute", {
      removalPolicy,
      logRetention,
      maxVcpus: props.jobCompute.maxVcpus,
      availabilityZones: SERVICE_SETTINGS.availabilityZones,
      natGateways: props.jobCompute.natGateways,
      updateTimeout: SERVICE_SETTINGS.computeEnvironmentUpdateTimeout,
    });
    const identities = new RuntimeIdentities(this, "RuntimeIdentities", {
      imageAccountIds: [serviceAccountId, ...allowedTenantAccountIds],
    });
    const bundle = createBackendBundle({ sourcePath: path.join(__dirname, "..", "..", "..") });
    const lambda = {
      bundle,
      memorySize: SERVICE_SETTINGS.lambdaMemoryMb,
      timeout: SERVICE_SETTINGS.lambdaTimeout,
      logRetention,
      removalPolicy,
    };
    // The API and ingestion consumer name this workflow before it exists: its role trusts them as the external ID.
    const destinationWorkflowArn = stateMachineArnFor(DestinationWorkflowName.STATE_MACHINE);

    this.destinationWorkflows = new DestinationWorkflows(this, "DestinationWorkflow", {
      ...accounts,
      destinationsTable: tables.destinationsTable,
      serviceKey,
      region: props.region,
      deletionProtectionThreshold: props.deletionProtectionThreshold,
      settings: SERVICE_SETTINGS.destinationWorkflow,
      logRetention,
      removalPolicy,
    });
    const ingestion = new IngestionQueue(this, "Ingestion", {
      ...accounts,
      ...lambda,
      tables,
      serviceKey,
      destinationWorkflowArn,
      // The connector deletion workflow sends to this queue, so a reference to it would make a cycle.
      connectorDeletionWorkflowArn: stateMachineArnFor(ConnectorDeletionWorkflowName.STATE_MACHINE),
      retention: queueRetention,
      settings: SERVICE_SETTINGS.ingestionQueue,
    });
    this.connectorDeletionWorkflow = new ConnectorDeletionWorkflow(
      this,
      "ConnectorDeletionWorkflow",
      {
        ...accounts,
        documentsTable: tables.documentsTable,
        collectionsTable: tables.collectionsTable,
        connectorsTable: tables.connectorsTable,
        ingestion,
        scheduleGroupName: delivery.schedulerGroup.scheduleGroupName,
        runtimeRoleArnPattern: identities.runtimeRoleArnPattern,
        logRetention,
        removalPolicy,
      },
    );
    const recordRetention = SERVICE_SETTINGS.jobRecordRetention;
    const activeRecordRetention = recordRetention.plus(
      Duration.seconds(numberRange({ schema: "ContainerProperties", member: "timeout" }).maximum),
    );
    this.jobDispatch = new JobDispatch(this, "JobDispatch", {
      tables,
      serviceKey,
      jobQueue: batch.jobQueue,
      batchExecutionRole: identities.batchExecutionRole,
      runtimeRoleArnPattern: identities.runtimeRoleArnPattern,
      ingestion,
      recordRetention,
      activeRecordRetention,
      logRetention,
      removalPolicy,
    });
    this.api = new ApiService(this, "ApiService", {
      ...accounts,
      tables,
      serviceKey,
      throttling: props.apiThrottling,
      logRetention,
      removalPolicy,
      runtimeRoleBoundary: identities.runtimeRoleBoundary,
      runtimeRoleArnPattern: identities.runtimeRoleArnPattern,
      schedulerGroupName: delivery.schedulerGroup.scheduleGroupName,
      schedulerTargetRole: delivery.schedulerTargetRole,
      schedulerTargetDlq: delivery.schedulerTargetDlq,
      destinationWorkflow: this.destinationWorkflows.creation,
      destinationDeletionWorkflow: this.destinationWorkflows.deletion,
      connectorDeletionWorkflow: this.connectorDeletionWorkflow.stateMachine,
      jobDispatch: this.jobDispatch,
      destinationWorkflowArn,
      bundle,
      memorySize: SERVICE_SETTINGS.lambdaMemoryMb,
      idempotencyExpirySeconds: SERVICE_SETTINGS.idempotencyExpirySeconds,
      schedulerRetryAttempts: SERVICE_SETTINGS.schedulerRetryAttempts,
      schedulerMaxEventAgeSeconds: SERVICE_SETTINGS.schedulerMaxEventAgeSeconds,
      jobs: {
        executionArn: this.jobDispatch.stateMachine.stateMachineArn,
        recordRetention,
        activeRecordRetention,
        executionRoleArn: identities.batchExecutionRole.roleArn,
        logRouterImage: SERVICE_SETTINGS.logRouterImage,
      },
    });

    const api = this.api;
    const outputs: FrameworkOutput[] = [
      {
        id: "ApiEndpoint",
        exportName: FrameworkExport.API_ENDPOINT,
        value: api.api.url,
        description:
          "The framework API endpoint; a tenant in another account copies it into its configuration.",
      },
      {
        id: "ServiceAccountId",
        exportName: FrameworkExport.SERVICE_ACCOUNT_ID,
        value: serviceAccountId,
        description: "The service account, which tenant trust policies name.",
      },
      {
        id: "BatchExecutionRoleArn",
        exportName: FrameworkExport.BATCH_EXECUTION_ROLE_ARN,
        value: identities.batchExecutionRole.roleArn,
        description: "The role that pulls connector images; tenant repository policies name it.",
      },
      {
        id: "DestinationWorkflowArn",
        exportName: FrameworkExport.DESTINATION_WORKFLOW_ARN,
        value: this.destinationWorkflows.creation.stateMachineArn,
        description:
          "CcfDestinationWorkflow, the external ID in every CcfDestinationRole trust policy.",
      },
      {
        id: "RestApiId",
        exportName: FrameworkExport.REST_API_ID,
        value: api.api.restApiId,
        description: "The framework API's ID, for exact execute-api ARNs.",
      },
      {
        id: "JobQueueArn",
        exportName: FrameworkExport.JOB_QUEUE_ARN,
        value: batch.jobQueue.jobQueueArn,
        description: "The Batch job queue connector jobs run in.",
      },
      {
        id: "ConnectorDeletionWorkflowArn",
        exportName: FrameworkExport.CONNECTOR_DELETION_WORKFLOW_ARN,
        value: this.connectorDeletionWorkflow.stateMachine.stateMachineArn,
        description: "CcfConnectorDeletionWorkflow, which the Ops stack alarms on.",
      },
      {
        id: "IngestionQueueName",
        exportName: FrameworkExport.INGESTION_QUEUE_NAME,
        value: ingestion.queue.queueName,
        description: "The queue of Bedrock ingestion requests.",
      },
      {
        id: "IngestionDlqName",
        exportName: FrameworkExport.INGESTION_DLQ_NAME,
        value: ingestion.deadLetterQueue.queueName,
        description: "Ingestion requests that failed for good.",
      },
      {
        id: "SchedulerGroupName",
        exportName: FrameworkExport.SCHEDULER_GROUP_NAME,
        value: delivery.schedulerGroup.scheduleGroupName,
        description: "The Scheduler group that holds custom connector schedules.",
      },
      {
        id: "SchedulerTargetDlqName",
        exportName: FrameworkExport.SCHEDULER_TARGET_DLQ_NAME,
        value: delivery.schedulerTargetDlq.queueName,
        description: "Scheduled starts that exhausted their retries.",
      },
      {
        id: "ApiLambdaName",
        exportName: FrameworkExport.API_LAMBDA_NAME,
        value: api.apiLambda.functionName,
        description: "The Lambda function behind the API.",
      },
      {
        id: "ScheduledStartLambdaName",
        exportName: FrameworkExport.SCHEDULED_START_LAMBDA_NAME,
        value: api.scheduledStartLambda.functionName,
        description: "The Lambda function Scheduler invokes to start a job.",
      },
      {
        id: "JobExecutionArn",
        exportName: FrameworkExport.JOB_EXECUTION_ARN,
        value: this.jobDispatch.stateMachine.stateMachineArn,
        description: "CcfJobExecution, which runs every job and which the Ops stack alarms on.",
      },
      {
        id: "IngestionConsumerLambdaName",
        exportName: FrameworkExport.INGESTION_CONSUMER_LAMBDA_NAME,
        value: ingestion.consumer.functionName,
        description: "The Lambda function that starts Bedrock ingestion jobs.",
      },
      {
        id: "ApiName",
        exportName: FrameworkExport.API_NAME,
        value: api.api.restApiName,
        description: "The API's name, the ApiName dimension of its metrics.",
      },
      {
        id: "ApiStageName",
        exportName: FrameworkExport.API_STAGE_NAME,
        value: api.api.deploymentStage.stageName,
        description: "The API's stage, the Stage dimension of its metrics.",
      },
    ];
    for (const { id: outputId, exportName, value, description } of outputs)
      new CfnOutput(this, outputId, { exportName, value, description });
  }
}
