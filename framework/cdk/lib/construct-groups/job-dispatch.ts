import { Aws, Duration, RemovalPolicy } from "aws-cdk-lib";
import { JobQueue } from "aws-cdk-lib/aws-batch";
import {
  Effect,
  IGrantable,
  IRole,
  PolicyStatement,
  Role,
  ServicePrincipal,
} from "aws-cdk-lib/aws-iam";
import { IKey } from "aws-cdk-lib/aws-kms";
import { RetentionDays } from "aws-cdk-lib/aws-logs";
import { StateMachine } from "aws-cdk-lib/aws-stepfunctions";
import { Construct } from "constructs";
import { BATCH_OWNERSHIP_TAG, JobExecutionName, ROLE_PATH } from "../common/config";
import { metricsContract } from "../common/contracts";
import {
  acknowledgeNagRules,
  resourceFindings,
  WORKFLOW_ROLE_STAR_ACKNOWLEDGMENT,
} from "../common/nag-suppressions";
import { DatabaseTables } from "./database-tables";
import { IngestionQueue } from "./ingestion-queue";
import { jobExecutionStates } from "./job-execution-states";
import { jsonataStateMachine } from "./workflow-states";

export interface JobDispatchProps {
  readonly tables: DatabaseTables;
  readonly serviceKey: IKey;
  readonly jobQueue: JobQueue;
  readonly batchExecutionRole: IRole;
  readonly runtimeRoleArnPattern: string;
  readonly ingestion: IngestionQueue;
  /** How long a job's record outlives the job's end. */
  readonly recordRetention: Duration;
  /** How long a running job's record lives: the record retention plus the longest job timeout. */
  readonly activeRecordRetention: Duration;
  readonly logRetention: RetentionDays;
  readonly removalPolicy: RemovalPolicy;
}

const batchArn = (resource: string): string =>
  `arn:${Aws.PARTITION}:batch:${Aws.REGION}:${Aws.ACCOUNT_ID}:${resource}`;
/** Every custom connector's job definition, by the framework's name prefix. */
export const JOB_DEFINITIONS = batchArn(`job-definition/${JobExecutionName.DEFINITION_PREFIX}*`);
const JOBS = batchArn("job/*");

/**
 * `CcfJobExecution`, which runs every job: it marks the job `RUNNING`, runs its Batch job and
 * waits, records the outcome, requests an ingestion, and publishes the job's metrics. Its role,
 * `CcfJobExecutionRole`, submits and stops only Batch jobs tagged as the framework's.
 */
export class JobDispatch extends Construct {
  public readonly stateMachine: StateMachine;
  public readonly role: Role;
  private readonly batchExecutionRole: IRole;
  private readonly runtimeRoleArnPattern: string;

  constructor(scope: Construct, id: string, props: JobDispatchProps) {
    const {
      tables,
      serviceKey,
      jobQueue,
      batchExecutionRole,
      runtimeRoleArnPattern,
      ingestion,
      recordRetention,
      activeRecordRetention,
      logRetention,
      removalPolicy,
    } = props;
    super(scope, id);
    this.batchExecutionRole = batchExecutionRole;
    this.runtimeRoleArnPattern = runtimeRoleArnPattern;
    this.role = new Role(this, "Role", {
      roleName: JobExecutionName.ROLE,
      path: ROLE_PATH,
      assumedBy: new ServicePrincipal("states.amazonaws.com").withConditions({
        StringEquals: { "aws:SourceAccount": Aws.ACCOUNT_ID },
        ArnLike: {
          "aws:SourceArn": `arn:${Aws.PARTITION}:states:${Aws.REGION}:${Aws.ACCOUNT_ID}:stateMachine:${JobExecutionName.STATE_MACHINE}`,
        },
      }),
      description: "Runs CcfJobExecution: one Batch job per custom connector job.",
    });
    serviceKey.grant(this.role, "kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey");
    ingestion.grantRequestIngestion(this.role);
    const owned = (condition: string): Record<string, Record<string, string>> => ({
      StringEquals: { [`aws:${condition}/${BATCH_OWNERSHIP_TAG.key}`]: BATCH_OWNERSHIP_TAG.value },
    });
    for (const statement of [
      // Batch authorizes tag-on-submit as batch:TagResource on the queue and definition.
      new PolicyStatement({
        actions: ["batch:SubmitJob", "batch:TagResource"],
        resources: [jobQueue.jobQueueArn, JOB_DEFINITIONS, JOBS],
        conditions: owned("RequestTag"),
      }),
      new PolicyStatement({
        actions: ["batch:TerminateJob"],
        resources: [JOBS],
        conditions: owned("ResourceTag"),
      }),
      // submitJob.sync waits through this managed rule, and reads the job with DescribeJobs.
      new PolicyStatement({
        actions: ["events:PutTargets", "events:PutRule", "events:DescribeRule"],
        resources: [
          `arn:${Aws.PARTITION}:events:${Aws.REGION}:${Aws.ACCOUNT_ID}:rule/StepFunctionsGetEventsForBatchJobsRule`,
        ],
      }),
      new PolicyStatement({ actions: ["batch:DescribeJobs"], resources: ["*"] }),
      // IAM authorizes each action of the finishing transaction as UpdateItem on its table.
      new PolicyStatement({
        actions: ["dynamodb:UpdateItem"],
        resources: [tables.jobsTable.tableArn, tables.connectorsTable.tableArn],
      }),
      new PolicyStatement({
        effect: Effect.DENY,
        actions: ["cloudwatch:PutMetricData"],
        resources: ["*"],
        conditions: { StringNotEquals: { "cloudwatch:namespace": metricsContract().namespace } },
      }),
    ])
      this.role.addToPolicy(statement);
    this.stateMachine = jsonataStateMachine(this, "StateMachine", {
      stateMachineName: JobExecutionName.STATE_MACHINE,
      role: this.role,
      logRetention,
      removalPolicy,
      definition: jobExecutionStates(this, {
        jobsTable: tables.jobsTable,
        connectorsTable: tables.connectorsTable,
        jobQueueArn: jobQueue.jobQueueArn,
        recordRetention,
        activeRecordRetention,
        ingestion,
      }),
    });
    acknowledgeNagRules({
      scopes: [this.role],
      acknowledgments: [
        WORKFLOW_ROLE_STAR_ACKNOWLEDGMENT,
        {
          id: "AwsSolutions-IAM5",
          reason:
            "Batch gives a job its ID at submission and the job definitions are named per custom connector, " +
            `so SubmitJob and TerminateJob name job/* and ${JobExecutionName.DEFINITION_PREFIX}* and require ` +
            "the framework's ownership tag; batch:DescribeJobs, which .sync calls, takes no resource.",
          findings: resourceFindings({ scope: this, resources: [JOBS, JOB_DEFINITIONS] }),
        },
      ],
    });
  }

  /** Register, tag, and deregister the custom connectors' job definitions, and pass them their roles. */
  public grantJobDefinitionRegistration(grantee: IGrantable): void {
    for (const statement of [
      new PolicyStatement({
        actions: ["batch:RegisterJobDefinition", "batch:TagResource"],
        resources: [JOB_DEFINITIONS],
      }),
      new PolicyStatement({
        actions: ["iam:PassRole"],
        resources: [this.batchExecutionRole.roleArn, this.runtimeRoleArnPattern],
        conditions: { StringEquals: { "iam:PassedToService": "ecs-tasks.amazonaws.com" } },
      }),
    ])
      grantee.grantPrincipal.addToPrincipalPolicy(statement);
    this.grantJobDefinitionDeregistration(grantee);
  }

  /** Deregister the custom connectors' job definitions, which Batch describes first. */
  public grantJobDefinitionDeregistration(grantee: IGrantable): void {
    for (const statement of [
      new PolicyStatement({
        actions: ["batch:DeregisterJobDefinition"],
        resources: [`${JOB_DEFINITIONS}:*`],
      }),
      // DescribeJobDefinitions supports no resource-level permission.
      new PolicyStatement({ actions: ["batch:DescribeJobDefinitions"], resources: ["*"] }),
    ])
      grantee.grantPrincipal.addToPrincipalPolicy(statement);
  }
}
