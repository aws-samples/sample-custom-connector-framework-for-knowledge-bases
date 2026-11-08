---
description: The Custom Connector Framework's alarms, dashboard, and metrics in the service account.
---

# Monitoring

The `CustomConnectorFrameworkOps` stack holds the framework's [Amazon CloudWatch](https://aws.amazon.com/cloudwatch/) alarms and the `CcfOperations` dashboard. Every alarm notifies the stack's [Amazon SNS](https://aws.amazon.com/sns/) topic, and its description starts with its severity and links to its section of the [runbook](runbook-delivery-failures.md). To receive the notifications, subscribe to the topic as [Deploy the service](deployment.md) describes.

## Alarms

Each alarm has a severity. P1 calls for a response now, and P2 is a degraded state to fix within the hour. Each alarm's name comes from its logical ID in [AWS CloudFormation](https://aws.amazon.com/cloudformation/), so each alarm ID below appears inside an alarm name, for example `CustomConnectorFrameworkOps-AlarmsJobsFailedAlarm...`.

| Alarm | Severity | Fires when |
| --- | --- | --- |
| `SchedulerDeliveryFailures` | P1 | [Amazon EventBridge Scheduler](https://docs.aws.amazon.com/scheduler/latest/UserGuide/what-is-scheduler.html) couldn't deliver a scheduled start |
| `SchedulerTargetDlqMessages`, `IngestionDlqMessages` | P1 | A message is in a dead-letter queue |
| `ApiLambdaErrors`, `ScheduledStartLambdaErrors`, `IngestionConsumerLambdaErrors` | P2 | A framework [AWS Lambda](https://aws.amazon.com/lambda/) function failed |
| `ApiServerErrorRate` | P2 | More than `alarms.apiServerErrorPercent`, 5% by default, of API requests failed with a server error |
| `ApiLatency` | P2 | The API's p99 latency is above `alarms.apiLatencyMs`, 5 seconds by default |
| `JobsFailed` | P2 | A job failed |
| `DestinationCreationFailed` | P2 | A destination failed to create |
| `DestinationWorkflowExecutionsFailed`, `ConnectorDeletionWorkflowExecutionsFailed`, `JobExecutionExecutionsFailed` | P2 | A workflow execution failed |

Every alarm evaluates five-minute periods. Most fire when two of three periods breach, so a passing spike doesn't notify anyone. The `JobsFailed`, `DestinationCreationFailed`, and `ExecutionsFailed` alarms fire on one period instead, because each of their datapoints is a failed job or a failed execution.

## Metrics

The framework publishes its metrics in the `CustomConnectorFramework` namespace, in the service account. Connector code publishes none. It writes JSON log events to its own log group in the tenant account instead.

| Metric | Dimensions | Published by |
| --- | --- | --- |
| `JobsCompleted` | `Status`; `Status`, `ConnectorId` | The job workflow, and the API for a job `StopCustomConnectorJob` ends |
| `JobDuration` | `ConnectorId` | The job workflow, and the API for a job `StopCustomConnectorJob` ends |
| `DocumentsRecorded` | `ConnectorId` | The API, for each successful `BatchPutCustomConnectorDocuments` |
| `DocumentsDeleted` | `ConnectorId` | The API, for each successful `BatchDeleteCustomConnectorDocuments` |
| `CollectionsDeleted` | `ConnectorId` | The API, for each successful `BatchDeleteCustomConnectorCollections` |
| `ApiErrors` | `Operation`, `ErrorCode` | The API, for each error response |
| `DestinationCreationFailed` | none | The destination workflow |
| `IngestionJobsStarted`, `IngestionJobsFailed` | none | The ingestion consumer |

`DocumentsDeleted` and `CollectionsDeleted` count the IDs a request sent, because the API doesn't read each item before deleting it.

## What tenants monitor

The framework's alarms and metrics aren't visible from a tenant account. Each tenant monitors its own jobs, logs, and knowledge base, as [Troubleshoot a job](../developer-guide/troubleshooting.md) describes. The example GitHub connector adds no alarm or metric. Its [operations runbook](../github-connector/operations.md) works from the log events in each custom connector's log group.
