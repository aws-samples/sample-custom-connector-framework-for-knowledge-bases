---
description: The runbook every Custom Connector Framework alarm links to, with the trigger, diagnosis, fix, and check for each.
---

# Runbook: delivery failures

Every alarm in the `CustomConnectorFrameworkOps` stack links to a section of this page. Each alarm notifies that stack's [Amazon SNS](https://aws.amazon.com/sns/) topic, its `AlarmTopicArnExport` output, when it changes to ALARM and back to OK. Each section gives the trigger, the steps to diagnose and fix it, and how to confirm the fix.

Start triage on the `CcfOperations` dashboard in the service account. It charts the framework's own metrics, such as jobs completed by status, job duration, API errors, and [Amazon Bedrock](https://aws.amazon.com/bedrock/) ingestion jobs. [Monitoring](monitoring.md) lists every alarm and metric.

Never paste connector configuration, a secret, an external ID, or document content into a ticket or a chat. The framework's log lines name operations, IDs, and error types, not values.

| Alarm | Severity | Section |
| --- | --- | --- |
| `SchedulerDeliveryFailures` | P1 | [scheduler-delivery](#scheduler-delivery) |
| `SchedulerTargetDlqMessages` | P1 | [delivery-dead-letter-queues](#delivery-dead-letter-queues) |
| `IngestionDlqMessages` | P1 | [ingestion-failures](#ingestion-failures) |
| `ApiLambdaErrors`, `ScheduledStartLambdaErrors`, `IngestionConsumerLambdaErrors` | P2 | [lambda-errors](#lambda-errors) |
| `ApiServerErrorRate`, `ApiLatency` | P2 | [ccf-api-errors-and-latency](#ccf-api-errors-and-latency) |
| `JobsFailed` | P2 | [failed-jobs](#failed-jobs) |
| `DestinationCreationFailed` | P2 | [destination-creation-failures](#destination-creation-failures) |
| `DestinationWorkflowExecutionsFailed`, `ConnectorDeletionWorkflowExecutionsFailed`, `JobExecutionExecutionsFailed` | P2 | [workflow-execution-failures](#workflow-execution-failures) |

## scheduler-delivery

**Severity: P1.** [Amazon EventBridge Scheduler](https://docs.aws.amazon.com/scheduler/latest/UserGuide/what-is-scheduler.html) couldn't invoke the scheduled-start [AWS Lambda](https://aws.amazon.com/lambda/) function for a custom connector's schedule, so a scheduled job didn't start. Scheduler retries, then sends the event to the scheduler target dead-letter queue.

1. Check the scheduled-start function's `Errors` and `Throttles` for the same period. A rise in `Throttles` means Lambda concurrency is used up.
1. Check that the scheduler target role, `CcfSchedulerTargetRole` in the framework stack, may still invoke the function and send to its dead-letter queue.
1. When the cause is fixed, replay the dead-letter queue's messages as [delivery-dead-letter-queues](#delivery-dead-letter-queues) describes.

The alarm returns to OK after deliveries stop failing.

## delivery-dead-letter-queues

**Severity: P1.** A scheduled start reached the scheduler target dead-letter queue after its retries, so a scheduled job never ran.

1. Find the failure in the scheduled-start function's log group.
1. Read the message's attributes without logging its body.
1. Fix the cause, then replay the message unchanged. A scheduled start carries the tenant ARN prefix, the destination ID, the connector ID, and `scheduled_time`. The scheduled time is part of the start's idempotency key, so a repeated delivery starts one job.
1. Confirm that the queue is empty and the job reached a final status.

## lambda-errors

**Severity: P2.** One of the framework's Lambda functions failed: the API, the scheduled start, or the ingestion consumer.

1. Open the function's log group and find the ERROR entries for the period. A missing or invalid setting fails the function at cold start by design.
1. If an AWS call was denied, compare the function's role with the synthesized template.
1. Fix the cause and redeploy `CustomConnectorFramework`. The alarm returns to OK when `Errors` returns to zero.

## ccf-api-errors-and-latency

**Severity: P2.** With the default thresholds, `ApiServerErrorRate` fires when more than 5% of API requests fail with a 5xx status in two of three five-minute periods. The `ApiLatency` alarm fires when p99 latency is above 5 seconds in two of three periods. The toolkit retries throttled requests, and retries server errors for operations that are safe to repeat, so a short spike delays jobs rather than failing them.

1. Compare the API function's `Errors`, `Throttles`, and `Duration` for the same period.
1. Check the `4XXError` count in [Amazon API Gateway](https://aws.amazon.com/api-gateway/). Many 429 responses mean callers exceed the stage limits. Raise `apiThrottling` in the framework configuration only after you confirm the load is expected.
1. In the API function's log group, find `api_operation_failed` entries (5xx) and `api_operation_rejected` entries (4xx), and group them by `operation` and `error_type`. A `DependencyThrottledError` (429) or `DependencyUnavailableError` (503) means an AWS call failed, and the entry's `aws_error_code` names the error AWS returned.
1. Confirm that the alarm returns to OK after the fix.

## failed-jobs

**Severity: P2.** A custom connector's job ended `FAILED`.

1. Read the job's `status_reason` with `GetCustomConnectorJob`, or list failures with `ListCustomConnectorJobs` and `status` `FAILED`. [Troubleshoot a job](../developer-guide/troubleshooting.md) maps each reason to its exit code and fix.
1. For exit codes 70 and 71, the reason names the log group and stream in the tenant account when the custom connector has a log destination. The tenant reads them there. Without a log destination, nothing the container printed was kept, and no connector output lands in the service account.
1. After the cause is fixed, start a job. The alarm returns to OK after one period without a failure.

## destination-creation-failures

**Severity: P2.** The destination workflow recorded a destination as `FAILED`.

1. Read the destination's `status_reason` with `GetDestination`. It has the form `<error>: <cause> (correlation <id>)`. A reason longer than 2,048 characters loses the middle of its cause, and the workflow's log events hold all of it.
1. Search the `/aws/vendedlogs/states/CcfDestinationWorkflow` log group for that correlation ID to find the failed state and its input.
1. Fix the cause. Common causes:
    - The destination's role, `CcfDestinationRole-<destination name>`, doesn't exist or doesn't trust the framework's roles.
    - The tenant account isn't in `allowedTenantAccountIds`.
    - A service quota, such as buckets or knowledge bases, is reached. The workflow already deleted what it created. A reason that says `cleanup incomplete` means that deletion failed too, so some of the destination's resources remain.
1. Delete the destination, which removes whatever remains, and create it again. For a destination stack, fix the cause and redeploy it.

## workflow-execution-failures

**Severity: P2.** An execution of `CcfDestinationWorkflow`, `CcfConnectorDeletionWorkflow`, or `CcfJobExecution` failed. Each state machine logs its errors to `/aws/vendedlogs/states/<state machine name>`.

- A failed delete leaves the destination or custom connector in `DELETING`: a step used up its retries.
- A failed create also fails its `CcfDestinationWorkflow` execution after it records the destination `FAILED`. The execution's cause ends with the correlation ID, and [destination-creation-failures](#destination-creation-failures) covers it.
- A `CcfJobExecution` execution fails with `JobNotFinished` when it couldn't record the job's end. The job keeps its last status, and its custom connector stays `IN_USE`.

1. In the [AWS Step Functions](https://aws.amazon.com/step-functions/) console, open the state machine's failed execution and read its error and cause.
1. Fix the cause, for example a destination role that was deleted or changed, or a throttled service.
1. For a delete, call the same delete operation again, `DeleteDestination` or `DeleteCustomConnector`. It starts a new execution, because the recorded one isn't running, and every step treats a resource that's already gone as done. Confirm with `GetDestination` or `GetCustomConnector`, which return `ResourceNotFoundException`.
1. For `JobNotFinished`, call `StopCustomConnectorJob` on the job, or start a new job, which stops it first. Confirm with `GetCustomConnectorJob` that the job is `STOPPED`.

## ingestion-failures

**Severity: P1.** An ingestion request for a `BEDROCK` destination reached the ingestion dead-letter queue. The consumer sends a request there right away on an access, not-found, or validation error. On any other error, such as Amazon Bedrock still running an earlier ingestion job on the data source, it retries up to 100 times, for about 24 hours. The knowledge base doesn't have the last job's documents indexed.

1. In the ingestion consumer's log group, find `ingestion_dead_lettered` and `ingestion_retry_scheduled` entries for the destination ID. Retries alone, with no `ingestion_dead_lettered`, mean the earlier ingestion job ran longer than the retries. Check its status with `ListIngestionJobs` before you change anything.
1. Check that the destination's knowledge base and data source still exist, and that its destination role trusts `CcfIngestionConsumerRole` with the `CcfDestinationWorkflow` ARN as external ID.
1. Fix the cause. The next successful job of any custom connector in the destination queues a new request. To start ingestion now, the tenant calls `StartIngestionJob` on the knowledge base.
1. Purge the dead-letter queue after the knowledge base is current.

When ingestion runs but documents are missing from search, the tenant reads the knowledge base's ingestion logs in `/aws/vendedlogs/bedrock/knowledge-base/ccf-<destination ID>`. This [CloudWatch Logs Insights](https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/AnalyzingLogData.html) query lists the documents that failed, with the reason Amazon Bedrock gives:

```text
filter event.connector_document_status.Status = "FAILED"
| fields event.document_id, event.source_uri, event.error_message, @timestamp
| sort @timestamp desc
```
