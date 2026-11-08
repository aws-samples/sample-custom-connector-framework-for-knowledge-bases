import { CfnOutput, Stack, StackProps } from "aws-cdk-lib";
import { Construct } from "constructs";
import { AlarmsConfig } from "../common/app-config";
import { FrameworkExport, importFrameworkValue } from "../common/framework-exports";
import { OperationsDashboard } from "../construct-groups/operations-dashboard";
import { FrameworkMonitoredResources, ServiceAlarms } from "../construct-groups/service-alarms";
import { AlarmTopic } from "../constructs/alarm-topic";

export interface CustomConnectorFrameworkOpsStackProps extends StackProps {
  /** The thresholds of the API alarms. */
  readonly alarms: AlarmsConfig;
}

/**
 * `CustomConnectorFrameworkOps`: the framework's alarms, the SNS topic they notify, and the
 * operations dashboard. It imports every resource it watches from the service stack's exports by
 * name, so it depends on that stack and deploys after it.
 */
export class CustomConnectorFrameworkOpsStack extends Stack {
  public readonly alarmTopic: AlarmTopic;

  constructor(scope: Construct, id: string, props: CustomConnectorFrameworkOpsStackProps) {
    super(scope, id, props);
    this.alarmTopic = new AlarmTopic(this, "AlarmTopic");
    new CfnOutput(this, "AlarmTopicArnExport", {
      exportName: FrameworkExport.ALARM_TOPIC_ARN,
      value: this.alarmTopic.topic.topicArn,
      description: "The topic every framework alarm notifies.",
    });
    const monitored: FrameworkMonitoredResources = {
      schedulerGroupName: importFrameworkValue(FrameworkExport.SCHEDULER_GROUP_NAME),
      schedulerTargetDlqName: importFrameworkValue(FrameworkExport.SCHEDULER_TARGET_DLQ_NAME),
      ingestionDlqName: importFrameworkValue(FrameworkExport.INGESTION_DLQ_NAME),
      apiLambdaName: importFrameworkValue(FrameworkExport.API_LAMBDA_NAME),
      scheduledStartLambdaName: importFrameworkValue(FrameworkExport.SCHEDULED_START_LAMBDA_NAME),
      ingestionConsumerLambdaName: importFrameworkValue(
        FrameworkExport.INGESTION_CONSUMER_LAMBDA_NAME,
      ),
      apiName: importFrameworkValue(FrameworkExport.API_NAME),
      apiStageName: importFrameworkValue(FrameworkExport.API_STAGE_NAME),
      destinationWorkflowArn: importFrameworkValue(FrameworkExport.DESTINATION_WORKFLOW_ARN),
      connectorDeletionWorkflowArn: importFrameworkValue(
        FrameworkExport.CONNECTOR_DELETION_WORKFLOW_ARN,
      ),
      jobExecutionArn: importFrameworkValue(FrameworkExport.JOB_EXECUTION_ARN),
    };
    new ServiceAlarms(this, "Alarms", {
      ...monitored,
      topic: this.alarmTopic.topic,
      apiLatencyAlarmMs: props.alarms.apiLatencyMs,
      apiServerErrorAlarmPercent: props.alarms.apiServerErrorPercent,
    });
    new OperationsDashboard(this, "Dashboard");
  }
}
