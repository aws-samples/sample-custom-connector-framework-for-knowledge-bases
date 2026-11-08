# Framework runbook

Every alarm in the `CustomConnectorFrameworkOps` stack names this file or one of
its sections, and notifies that stack's SNS topic (its `AlarmTopicArnExport`
output) on ALARM and on OK. Each section gives the trigger, the steps to
diagnose and fix it, and how to confirm the fix. Start triage on the
`CcfOperations` dashboard in the service account. It charts the framework's
own metrics: jobs completed by status, job duration, the documents and
collections the API recorded, API errors, destination creation failures, and
Bedrock ingestion jobs.

Never paste connector configuration, a secret, an external ID, or document
content into a ticket or a chat. The framework's log lines name operations,
IDs, and error types, never values.

Each alarm ID below appears inside the alarm's CloudFormation-generated name.

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

**Severity: P1.** EventBridge Scheduler couldn't invoke the scheduled-start
Lambda for a custom connector's schedule, so a scheduled job didn't start.
Scheduler retries, then sends the event to the scheduler target dead-letter
queue.

1. Check the scheduled-start Lambda's `Errors` and `Throttles` for the same
   period. A rise in `Throttles` means Lambda concurrency is exhausted.
1. Check that the scheduler target role (`CcfSchedulerTargetRole` in the
   framework stack) may still invoke the Lambda and send to its dead-letter
   queue.
1. When the cause is fixed, replay the dead-letter queue's messages as
   [delivery-dead-letter-queues](#delivery-dead-letter-queues) describes.

The alarm returns to OK when no delivery fails for one period.

## delivery-dead-letter-queues

**Severity: P1.** A scheduled start reached the scheduler target dead-letter
queue after its retries, so a scheduled job never ran.

1. Find the failure in the scheduled-start Lambda's log group.
1. Read the message's attributes without logging its body.
1. Fix the cause, then replay. A scheduled start carries the tenant ARN prefix,
   destination ID, connector ID, and `scheduled_time`. Replay it unchanged:
   the scheduled time is part of the start's idempotency key, so a repeated
   delivery starts one job.
1. Confirm the queue is empty and the job reached a final status.

## lambda-errors

**Severity: P2.** One of the framework's Lambda functions failed: the API, the
scheduled start, or the ingestion consumer.

1. Open the function's log group and find the ERROR entries for the period.
   A missing or invalid setting fails the function at cold start by design.
1. Compare the function's role with the synthesized template if an AWS call
   was denied.
1. Fix the cause and redeploy `CustomConnectorFramework`. The alarm returns to
   OK when `Errors` returns to zero.

## ccf-api-errors-and-latency

**Severity: P2.** `ApiServerErrorRate` fires when more than 5% of API requests in
a five-minute period fail with a 5XX status. `ApiLatency` fires when p99
latency exceeds 5 seconds. The toolkit retries throttled requests, and retries
server errors for operations that are safe to repeat, so a short spike delays
jobs rather than failing them.

1. Compare the API Lambda's `Errors`, `Throttles`, and `Duration` for the same
   period.
1. Check API Gateway's `4XXError` count. Many 429 responses mean callers exceed
   the stage limits; raise `apiThrottling` in the framework configuration only
   after confirming the load is expected.
1. In the API Lambda's log group, find `api_operation_failed` entries and group
   them by `operation` and `error_type`. A `StorageThrottledError` or
   `StorageOperationError` points at DynamoDB.
1. Confirm the alarm returns to OK after the fix.

## failed-jobs

**Severity: P2.** A custom connector's job ended `FAILED`.

1. Read the job's `status_reason` with `GetCustomConnectorJob`, or list
   failures with `ListCustomConnectorJobs` and `status` `FAILED`. The
   [administrator guide](../../../docs/administrator-guide.md#troubleshoot-a-failed-job)
   maps each reason to its exit code and fix.
1. For exit codes 70 and 71, the reason names the log group and stream in the
   tenant account when the custom connector has a log destination. Read them
   there. Without a log destination, nothing the container printed was kept,
   and no connector output ever lands in the service account.
1. Fix the cause and start a job. The alarm returns to OK after one period
   without a failure.

## destination-creation-failures

**Severity: P2.** The destination workflow recorded a destination as `FAILED`.

1. Read the destination's `status_reason` with `GetDestination`. It has the form
   `<error>: <cause> (correlation <id>)`. A reason longer than 2,048 characters
   loses the middle of its cause; the workflow's log events hold all of it.
1. Search the `/aws/vendedlogs/states/CcfDestinationWorkflow` log group for
   that correlation ID to find the failed state and its input.
1. Fix the cause. Common ones: the destination's role
   (`CcfDestinationRole-<destination name>`) doesn't exist or doesn't trust the
   framework's roles, the tenant account isn't in `allowedTenantAccountIds`, or
   a service quota (buckets, knowledge bases) is reached, in which case the
   workflow already deleted what it created. A reason that says
   `cleanup incomplete` means that deletion itself failed, so some of the
   destination's resources remain.
1. Delete the destination, which removes whatever remains, and create it
   again. For a `CustomConnectorDestinationFor<Name>` stack, fix the cause and
   redeploy it.

## workflow-execution-failures

**Severity: P2.** An execution of `CcfDestinationWorkflow`,
`CcfConnectorDeletionWorkflow`, or `CcfJobExecution` failed. Each state
machine logs its errors to `/aws/vendedlogs/states/<state machine name>`.

- A failed delete leaves the destination or custom connector in `DELETING`: a
  step exhausted its retries.
- A failed create also fails its `CcfDestinationWorkflow` execution after it
  records the destination `FAILED`. The execution's cause ends with the
  correlation ID, and
  [destination-creation-failures](#destination-creation-failures) covers it.
- A `CcfJobExecution` execution fails with `JobNotFinished` when it couldn't
  record the job's end. The job keeps its last status, and its custom
  connector stays `IN_USE`.

1. In the Step Functions console, open the state machine's failed execution
   and read its error and cause.
1. Fix the cause, for example a destination role that was deleted or
   changed, or a throttled service.
1. For a delete, call the same delete operation again: `DeleteDestination` or
   `DeleteCustomConnector`. It starts a new execution, because the recorded one
   isn't running, and every step treats a resource that is already gone as
   done. Confirm the resource is gone with `GetDestination` or
   `GetCustomConnector`, which return `ResourceNotFoundException`.
1. For `JobNotFinished`, call `StopCustomConnectorJob` on the job, or start a
   new job, which stops it first. The
   [user guide](../../../docs/user-guide.md#jobs) describes both. Confirm with
   `GetCustomConnectorJob` that the job is `STOPPED`.

## ingestion-failures

**Severity: P1.** An ingestion request for a `BEDROCK` destination reached the
ingestion dead-letter queue. The consumer sends a request there right away on
an access, not-found, or validation error. Otherwise it retries 100 times,
about 24 hours, while Bedrock still runs an earlier ingestion job on the data
source. The knowledge base doesn't have the last job's documents indexed.

1. In the ingestion consumer's log group, find `ingestion_dead_lettered` and
   `ingestion_retry_scheduled` entries for the destination ID. Retries alone,
   with no `ingestion_dead_lettered`, mean the earlier ingestion job ran longer
   than the retries; check its status with `ListIngestionJobs` before you
   change anything.
1. Check that the destination's knowledge base and data source still exist,
   and that its destination role trusts `CcfIngestionConsumerRole` with the
   `CcfDestinationWorkflow` ARN as external ID.
1. Fix the cause. The next successful job of any custom connector in the
   destination queues a new request. To start ingestion now, call
   `StartIngestionJob` on the knowledge base in the tenant account.
1. Purge the dead-letter queue after the knowledge base is current.

When ingestion runs but documents are missing from search, read the
knowledge base's ingestion logs, in the destination's account, in
`/aws/vendedlogs/bedrock/knowledge-base/ccf-<destination ID>`. This
CloudWatch Logs Insights query lists the documents that failed, with
Bedrock's reason:

```text
filter event.connector_document_status.Status = "FAILED"
| fields event.document_id, event.source_uri, event.error_message, @timestamp
| sort @timestamp desc
```
