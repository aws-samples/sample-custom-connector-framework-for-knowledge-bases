import { Duration } from "aws-cdk-lib";
import { Alarm, ComparisonOperator, IMetric, TreatMissingData } from "aws-cdk-lib/aws-cloudwatch";
import { SnsAction } from "aws-cdk-lib/aws-cloudwatch-actions";
import { ITopic } from "aws-cdk-lib/aws-sns";
import { Construct } from "constructs";

/** P1 pages on-call; P2 is a degraded state the owning team works within the hour. */
export enum AlarmSeverity {
  P1 = "P1",
  P2 = "P2",
}

/** Every alarm uses five-minute periods and alarms on two of three, unless it says otherwise. */
export const ALARM_PERIOD = Duration.minutes(5);
const DEFAULT_EVALUATION_PERIODS = 3;
const DEFAULT_DATAPOINTS = 2;

export interface SeverityAlarmProps {
  readonly metric: IMetric;
  readonly severity: AlarmSeverity;
  /** What the alarm means, in one sentence. */
  readonly summary: string;
  /** The runbook URL and section, such as `<DELIVERY_RUNBOOK>#failed-jobs`. */
  readonly runbook: string;
  readonly topic: ITopic;
  /** Defaults to 0 with `GREATER_THAN_THRESHOLD`. */
  readonly threshold?: number;
  readonly comparisonOperator?: ComparisonOperator;
  readonly evaluationPeriods?: number;
  readonly datapointsToAlarm?: number;
  /** Defaults to `NOT_BREACHING`, so a quiet period never raises the alarm. */
  readonly treatMissingData?: TreatMissingData;
}

/**
 * A CloudWatch alarm whose description starts with its severity and ends with its runbook
 * section, and that notifies one SNS topic on ALARM and on OK.
 */
export class SeverityAlarm extends Construct {
  public readonly alarm: Alarm;

  constructor(scope: Construct, id: string, props: SeverityAlarmProps) {
    super(scope, id);
    const {
      metric,
      severity,
      summary,
      runbook,
      topic,
      threshold,
      comparisonOperator,
      evaluationPeriods,
      datapointsToAlarm,
      treatMissingData,
    } = props;
    this.alarm = new Alarm(this, "Alarm", {
      metric,
      threshold: threshold ?? 0,
      comparisonOperator: comparisonOperator ?? ComparisonOperator.GREATER_THAN_THRESHOLD,
      evaluationPeriods: evaluationPeriods ?? DEFAULT_EVALUATION_PERIODS,
      datapointsToAlarm: datapointsToAlarm ?? DEFAULT_DATAPOINTS,
      treatMissingData: treatMissingData ?? TreatMissingData.NOT_BREACHING,
      alarmDescription: `[${severity}] ${summary} Runbook: ${runbook}`,
    });
    this.alarm.addAlarmAction(new SnsAction(topic));
    this.alarm.addOkAction(new SnsAction(topic));
  }
}
