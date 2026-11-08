import { Fn } from "aws-cdk-lib";

/** The two framework stacks. The Ops stack imports the service stack's exports, so it deploys second. */
export enum FrameworkStackName {
  SERVICE = "CustomConnectorFramework",
  OPS = "CustomConnectorFrameworkOps",
}

/**
 * The CloudFormation export names the framework stacks publish. Other
 * stacks read these values only through `Fn.importValue`, so this enum is the whole contract
 * between the framework and the stacks built on it. CloudFormation refuses to delete or change
 * an export while another stack imports it. Export names are unique per account and Region.
 */
export enum FrameworkExport {
  // The four values a tenant needs. A tenant in another account copies the
  // matching stack outputs into its configuration, because exports can't cross accounts.
  API_ENDPOINT = "CustomConnectorFramework-ApiEndpoint",
  SERVICE_ACCOUNT_ID = "CustomConnectorFramework-ServiceAccountId",
  BATCH_EXECUTION_ROLE_ARN = "CustomConnectorFramework-BatchExecutionRoleArn",
  DESTINATION_WORKFLOW_ARN = "CustomConnectorFramework-DestinationWorkflowArn",

  // Read by tenant constructs when `frameworkEndpoint` is left out, for exact execute-api ARNs.
  REST_API_ID = "CustomConnectorFramework-RestApiId",

  // Connector compute.
  JOB_QUEUE_ARN = "CustomConnectorFramework-JobQueueArn",

  // What CustomConnectorFrameworkOps alarms on and charts.
  CONNECTOR_DELETION_WORKFLOW_ARN = "CustomConnectorFramework-ConnectorDeletionWorkflowArn",
  INGESTION_QUEUE_NAME = "CustomConnectorFramework-IngestionQueueName",
  INGESTION_DLQ_NAME = "CustomConnectorFramework-IngestionDlqName",
  SCHEDULER_GROUP_NAME = "CustomConnectorFramework-SchedulerGroupName",
  SCHEDULER_TARGET_DLQ_NAME = "CustomConnectorFramework-SchedulerTargetDlqName",
  API_LAMBDA_NAME = "CustomConnectorFramework-ApiLambdaName",
  SCHEDULED_START_LAMBDA_NAME = "CustomConnectorFramework-ScheduledStartLambdaName",
  JOB_EXECUTION_ARN = "CustomConnectorFramework-JobExecutionArn",
  INGESTION_CONSUMER_LAMBDA_NAME = "CustomConnectorFramework-IngestionConsumerLambdaName",
  API_NAME = "CustomConnectorFramework-ApiName",
  API_STAGE_NAME = "CustomConnectorFramework-ApiStageName",

  // Published by CustomConnectorFrameworkOps: the topic every framework alarm notifies.
  ALARM_TOPIC_ARN = "CustomConnectorFrameworkOps-AlarmTopicArn",
}

/** A token that resolves, at deploy time, to the value exported under `name`. */
export const importFrameworkValue = (name: FrameworkExport): string => Fn.importValue(name);
