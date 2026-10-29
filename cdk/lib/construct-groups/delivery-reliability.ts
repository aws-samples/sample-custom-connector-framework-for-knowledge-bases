import { Duration, RemovalPolicy } from "aws-cdk-lib";
import { Alarm, ComparisonOperator, Metric, TreatMissingData } from "aws-cdk-lib/aws-cloudwatch";
import { Role, ServicePrincipal } from "aws-cdk-lib/aws-iam";
import { IKey } from "aws-cdk-lib/aws-kms";
import { IFunction } from "aws-cdk-lib/aws-lambda";
import { ScheduleGroup } from "aws-cdk-lib/aws-scheduler";
import { IQueue, Queue, QueueEncryption } from "aws-cdk-lib/aws-sqs";
import { Construct } from "constructs";
import { ProjectName, ResourceName, createConstructId, createResourceName } from "../common/config";

const RUNBOOK_URL =
  "https://github.com/aws-samples/sample-amazon-q-business-custom-connector-framework/blob/main/docs/runbooks/delivery-failures.md";
const ALARM_PERIOD = Duration.minutes(5);

export interface DeliveryResourcesProps {
  readonly projectName: ProjectName;
  readonly serviceKey: IKey;
  readonly removalPolicy: RemovalPolicy;
}

/** Encrypted failure queues plus the deterministic Scheduler group and target role. */
export class DeliveryResources extends Construct {
  public readonly schedulerGroup: ScheduleGroup;
  public readonly schedulerTargetRole: Role;
  public readonly schedulerTargetDlq: Queue;
  public readonly jobsStreamDlq: Queue;
  public readonly batchStatusDlq: Queue;

  constructor(scope: Construct, id: string, props: DeliveryResourcesProps) {
    super(scope, id);

    const { projectName, serviceKey, removalPolicy } = props;
    this.schedulerTargetDlq = this.createQueue(
      ResourceName.SCHEDULER_TARGET_DLQ,
      serviceKey,
      removalPolicy,
    );
    this.jobsStreamDlq = this.createQueue(ResourceName.JOBS_STREAM_DLQ, serviceKey, removalPolicy);
    this.batchStatusDlq = this.createQueue(
      ResourceName.BATCH_STATUS_DLQ,
      serviceKey,
      removalPolicy,
    );
    this.schedulerGroup = new ScheduleGroup(this, createConstructId(ResourceName.SCHEDULER_GROUP), {
      scheduleGroupName: createResourceName(projectName, ResourceName.SCHEDULER_GROUP),
    });
    this.schedulerTargetRole = new Role(
      this,
      createConstructId(ResourceName.SCHEDULER_TARGET_ROLE),
      {
        roleName: createResourceName(projectName, ResourceName.SCHEDULER_TARGET_ROLE),
        assumedBy: new ServicePrincipal("scheduler.amazonaws.com"),
        description: "Invokes the one framework scheduled-start target.",
      },
    );
    this.schedulerTargetDlq.grantSendMessages(this.schedulerTargetRole);
  }

  private createQueue(name: ResourceName, serviceKey: IKey, removalPolicy: RemovalPolicy): Queue {
    return new Queue(this, createConstructId(name), {
      queueName: createResourceName(ProjectName.CCF, name),
      encryption: QueueEncryption.KMS,
      encryptionMasterKey: serviceKey,
      enforceSSL: true,
      retentionPeriod: Duration.days(14),
      removalPolicy,
    });
  }
}

export interface ServiceAlarmsProps {
  readonly schedulerGroupName: string;
  readonly schedulerTargetDlq: IQueue;
  readonly jobsStreamDlq: IQueue;
  readonly batchStatusDlq: IQueue;
  readonly apiLambda: IFunction;
  readonly scheduledStartLambda: IFunction;
  readonly jobOrchestratorLambda: IFunction;
  readonly jobStatusLambda: IFunction;
}

/** Customer-impact and delivery-failure alarms for the Task 7 asynchronous paths. */
export class ServiceAlarms extends Construct {
  constructor(scope: Construct, id: string, props: ServiceAlarmsProps) {
    super(scope, id);

    const {
      schedulerGroupName,
      schedulerTargetDlq,
      jobsStreamDlq,
      batchStatusDlq,
      apiLambda,
      scheduledStartLambda,
      jobOrchestratorLambda,
      jobStatusLambda,
    } = props;

    this.createSchedulerDeliveryAlarm(schedulerGroupName);
    for (const [name, queue] of [
      ["SchedulerTarget", schedulerTargetDlq],
      ["JobsStream", jobsStreamDlq],
      ["BatchStatus", batchStatusDlq],
    ] as const) {
      this.createDlqAlarm(name, queue);
    }
    for (const [name, handler] of [
      ["Api", apiLambda],
      ["ScheduledStart", scheduledStartLambda],
      ["JobOrchestrator", jobOrchestratorLambda],
      ["JobStatus", jobStatusLambda],
    ] as const) {
      this.createLambdaErrorAlarm(name, handler);
    }
  }

  private createSchedulerDeliveryAlarm(schedulerGroupName: string): void {
    new Alarm(this, "SchedulerDeliveryFailures", {
      metric: new Metric({
        namespace: "AWS/Scheduler",
        metricName: "TargetErrorCount",
        dimensionsMap: { ScheduleGroup: schedulerGroupName },
        statistic: "Sum",
        period: ALARM_PERIOD,
      }),
      threshold: 0,
      comparisonOperator: ComparisonOperator.GREATER_THAN_THRESHOLD,
      evaluationPeriods: 3,
      datapointsToAlarm: 2,
      treatMissingData: TreatMissingData.NOT_BREACHING,
      alarmDescription: `[P1] Scheduler could not deliver connector starts. Runbook: ${RUNBOOK_URL}`,
    });
  }

  private createDlqAlarm(name: string, queue: IQueue): void {
    new Alarm(this, `${name}DlqMessages`, {
      metric: queue.metricApproximateNumberOfMessagesVisible({
        statistic: "Maximum",
        period: ALARM_PERIOD,
      }),
      threshold: 0,
      comparisonOperator: ComparisonOperator.GREATER_THAN_THRESHOLD,
      evaluationPeriods: 3,
      datapointsToAlarm: 2,
      treatMissingData: TreatMissingData.NOT_BREACHING,
      alarmDescription: `[P1] ${name} delivery exhausted retries. Runbook: ${RUNBOOK_URL}`,
    });
  }

  private createLambdaErrorAlarm(name: string, handler: IFunction): void {
    new Alarm(this, `${name}LambdaErrors`, {
      metric: handler.metricErrors({ statistic: "Sum", period: ALARM_PERIOD }),
      threshold: 0,
      comparisonOperator: ComparisonOperator.GREATER_THAN_THRESHOLD,
      evaluationPeriods: 3,
      datapointsToAlarm: 2,
      treatMissingData: TreatMissingData.NOT_BREACHING,
      alarmDescription: `[P2] ${name} Lambda errors detected. Runbook: ${RUNBOOK_URL}`,
    });
  }
}
