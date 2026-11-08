import { MathExpression, Metric } from "aws-cdk-lib/aws-cloudwatch";
import { ITopic } from "aws-cdk-lib/aws-sns";
import { Construct } from "constructs";
import {
  enumMember,
  frameworkDimensionName,
  frameworkMetricName,
  metricsContract,
} from "../common/contracts";
import { ALARM_PERIOD, AlarmSeverity, SeverityAlarm } from "../constructs/severity-alarm";

/** The repository path of the runbook every framework alarm links to. */
export const DELIVERY_RUNBOOK = "framework/docs/runbooks/delivery-failures.md";

/** The runbook sections the framework's alarms link to. */
export enum DeliveryRunbookSection {
  SCHEDULER_DELIVERY = "scheduler-delivery",
  DEAD_LETTER_QUEUES = "delivery-dead-letter-queues",
  LAMBDA_ERRORS = "lambda-errors",
  API = "ccf-api-errors-and-latency",
  FAILED_JOBS = "failed-jobs",
  DESTINATION_CREATION = "destination-creation-failures",
  WORKFLOW_EXECUTIONS = "workflow-execution-failures",
  INGESTION = "ingestion-failures",
}

/** The names of the framework resources the Ops stack watches, read from the service stack's exports. */
export interface FrameworkMonitoredResources {
  readonly schedulerGroupName: string;
  readonly schedulerTargetDlqName: string;
  readonly ingestionDlqName: string;
  readonly apiLambdaName: string;
  readonly scheduledStartLambdaName: string;
  readonly ingestionConsumerLambdaName: string;
  readonly apiName: string;
  readonly apiStageName: string;
  readonly destinationWorkflowArn: string;
  readonly connectorDeletionWorkflowArn: string;
  readonly jobExecutionArn: string;
}

export interface ServiceAlarmsProps extends FrameworkMonitoredResources {
  /** Every alarm notifies this topic on ALARM and on OK. */
  readonly topic: ITopic;
  /** The API's p99 latency above which its alarm fires. */
  readonly apiLatencyAlarmMs: number;
  /** The API's 5xx percentage above which its alarm fires. */
  readonly apiServerErrorAlarmPercent: number;
}

/** A single event is the failure, so the alarm fires on its first datapoint. */
const SINGLE_EVENT = { evaluationPeriods: 1, datapointsToAlarm: 1 } as const;

interface SummedMetric {
  readonly namespace: string;
  readonly metricName: string;
  readonly dimensionsMap?: Record<string, string>;
}

const sum = (metric: SummedMetric): Metric =>
  new Metric({ ...metric, statistic: "Sum", period: ALARM_PERIOD });

const runbook = (section: DeliveryRunbookSection): string => `${DELIVERY_RUNBOOK}#${section}`;

/**
 * The framework's alarms: delivery, the dead-letter queues, each Lambda's errors, the API's server
 * errors and latency, failed jobs, failed destination creations, and the two workflows' failed
 * executions. Built from resource names, not constructs: the Ops stack imports the service stack's
 * exports by name, so it depends on that stack and deploys after it.
 */
export class ServiceAlarms extends Construct {
  constructor(scope: Construct, id: string, props: ServiceAlarmsProps) {
    super(scope, id);
    this.deliveryAlarms(props);
    this.lambdaAlarms(props);
    this.apiAlarms(props);
    this.frameworkAlarms(props);
    this.workflowAlarms(props);
  }

  private deliveryAlarms(props: ServiceAlarmsProps): void {
    const { topic } = props;
    new SeverityAlarm(this, "SchedulerDeliveryFailures", {
      metric: sum({
        namespace: "AWS/Scheduler",
        metricName: "TargetErrorCount",
        dimensionsMap: { ScheduleGroup: props.schedulerGroupName },
      }),
      severity: AlarmSeverity.P1,
      summary: "Scheduler could not deliver custom connector starts.",
      runbook: runbook(DeliveryRunbookSection.SCHEDULER_DELIVERY),
      topic,
    });
    for (const [name, queueName, section] of [
      ["SchedulerTarget", props.schedulerTargetDlqName, DeliveryRunbookSection.DEAD_LETTER_QUEUES],
      ["Ingestion", props.ingestionDlqName, DeliveryRunbookSection.INGESTION],
    ] as const)
      new SeverityAlarm(this, `${name}DlqMessages`, {
        metric: new Metric({
          namespace: "AWS/SQS",
          metricName: "ApproximateNumberOfMessagesVisible",
          dimensionsMap: { QueueName: queueName },
          statistic: "Maximum",
          period: ALARM_PERIOD,
        }),
        severity: AlarmSeverity.P1,
        summary: `${name} delivery exhausted its retries.`,
        runbook: runbook(section),
        topic,
      });
  }

  private lambdaAlarms(props: ServiceAlarmsProps): void {
    for (const [name, functionName] of [
      ["Api", props.apiLambdaName],
      ["ScheduledStart", props.scheduledStartLambdaName],
      ["IngestionConsumer", props.ingestionConsumerLambdaName],
    ] as const)
      new SeverityAlarm(this, `${name}LambdaErrors`, {
        metric: sum({
          namespace: "AWS/Lambda",
          metricName: "Errors",
          dimensionsMap: { FunctionName: functionName },
        }),
        severity: AlarmSeverity.P2,
        summary: `The ${name} Lambda function failed.`,
        runbook: runbook(DeliveryRunbookSection.LAMBDA_ERRORS),
        topic: props.topic,
      });
  }

  private apiAlarms(props: ServiceAlarmsProps): void {
    const { topic, apiServerErrorAlarmPercent, apiLatencyAlarmMs } = props;
    const dimensionsMap = { ApiName: props.apiName, Stage: props.apiStageName };
    new SeverityAlarm(this, "ApiServerErrorRate", {
      metric: new MathExpression({
        expression: "IF(requests > 0, 100 * errors / requests, 0)",
        usingMetrics: {
          errors: sum({ namespace: "AWS/ApiGateway", metricName: "5XXError", dimensionsMap }),
          requests: sum({ namespace: "AWS/ApiGateway", metricName: "Count", dimensionsMap }),
        },
        period: ALARM_PERIOD,
        label: "Framework API server error percent",
      }),
      severity: AlarmSeverity.P2,
      summary: `More than ${apiServerErrorAlarmPercent}% of framework API requests failed with a server error.`,
      runbook: runbook(DeliveryRunbookSection.API),
      topic,
      threshold: apiServerErrorAlarmPercent,
    });
    new SeverityAlarm(this, "ApiLatency", {
      metric: new Metric({
        namespace: "AWS/ApiGateway",
        metricName: "Latency",
        dimensionsMap,
        statistic: "p99",
        period: ALARM_PERIOD,
      }),
      severity: AlarmSeverity.P2,
      summary: `Framework API p99 latency is above ${apiLatencyAlarmMs} ms.`,
      runbook: runbook(DeliveryRunbookSection.API),
      topic,
      threshold: apiLatencyAlarmMs,
    });
  }

  private frameworkAlarms({ topic }: ServiceAlarmsProps): void {
    const namespace = metricsContract().namespace;
    new SeverityAlarm(this, "JobsFailed", {
      metric: sum({
        namespace,
        metricName: frameworkMetricName("JOBS_COMPLETED"),
        dimensionsMap: {
          [frameworkDimensionName("STATUS")]: enumMember({ schema: "JobStatus", member: "FAILED" }),
        },
      }),
      severity: AlarmSeverity.P2,
      summary: "A custom connector job failed; its status_reason says why.",
      runbook: runbook(DeliveryRunbookSection.FAILED_JOBS),
      topic,
      ...SINGLE_EVENT,
    });
    new SeverityAlarm(this, "DestinationCreationFailed", {
      metric: sum({ namespace, metricName: frameworkMetricName("DESTINATION_CREATION_FAILED") }),
      severity: AlarmSeverity.P2,
      summary:
        "A destination failed to create; its status_reason carries the error and the correlation ID.",
      runbook: runbook(DeliveryRunbookSection.DESTINATION_CREATION),
      topic,
      ...SINGLE_EVENT,
    });
  }

  private workflowAlarms(props: ServiceAlarmsProps): void {
    for (const [name, stateMachineArn, summary] of [
      [
        "DestinationWorkflow",
        props.destinationWorkflowArn,
        "A DestinationWorkflow execution failed: a create that recorded its destination FAILED, whose cause " +
          "names the correlation ID, or a delete; read its error, fix the cause, and for a delete call it again.",
      ],
      [
        "ConnectorDeletionWorkflow",
        props.connectorDeletionWorkflowArn,
        "A ConnectorDeletionWorkflow execution failed; read its error, fix the cause, and call the same delete operation again.",
      ],
      [
        "JobExecution",
        props.jobExecutionArn,
        "A JobExecution execution failed with JobNotFinished: the job's end wasn't recorded; read its error, " +
          "and stop the job through the API to release its custom connector.",
      ],
    ] as const)
      new SeverityAlarm(this, `${name}ExecutionsFailed`, {
        metric: sum({
          namespace: "AWS/States",
          metricName: "ExecutionsFailed",
          dimensionsMap: { StateMachineArn: stateMachineArn },
        }),
        severity: AlarmSeverity.P2,
        summary,
        runbook: runbook(DeliveryRunbookSection.WORKFLOW_EXECUTIONS),
        topic: props.topic,
        ...SINGLE_EVENT,
      });
  }
}
