# CCF delivery failure runbook

## Scope and severity

This runbook covers the framework [Amazon EventBridge
Scheduler](https://aws.amazon.com/eventbridge/) target, jobs-table stream
consumer, terminal [AWS Batch](https://aws.amazon.com/batch/) event target,
and their dead-letter queues (DLQs).

- **P1:** Scheduler delivery failures or any message in a delivery DLQ. Connector starts or terminal reconciliation may be lost after retries.
- **P2:** Repeated API, scheduled-start, job-orchestrator, or job-status [AWS Lambda](https://aws.amazon.com/lambda/) errors.

## Likely causes

1. The target Lambda is throttled, unavailable, or denied invocation.
2. The target role or event-source mapping cannot send to its encrypted DLQ.
3. A malformed event or downstream AWS service failure continues beyond the configured retry limit.

## Immediate actions

1. Identify the alarmed path and inspect its Lambda `Errors`, `Throttles`, and duration metrics for the same period.
2. Inspect the corresponding DLQ message metadata without logging message bodies or customer configuration.
3. Verify the Scheduler target role, Lambda execution role, queue policy, and
   [AWS Key Management Service (AWS KMS)](https://aws.amazon.com/kms/) grants
   still match the synthesized stack.
4. For jobs-stream failures, compare the
   [Amazon DynamoDB](https://aws.amazon.com/dynamodb/) stream record identity
   with the persisted job state before replay.
5. For terminal Batch failures, verify the Batch job state and persisted CCF job state before replay.
6. For job-definition cleanup failures, verify the job-orchestrator and job-status Lambda execution roles have `batch:DescribeJobDefinitions` on `*` and `batch:DeregisterJobDefinition` on `arn:${Partition}:batch:${Region}:${Account}:job-definition/ccj-*:*`.
7. For Scheduler failures, verify the schedule group, target ARN, target role ARN, retry policy, and maximum event age. Scheduler delivery is at least once, and the scheduled payload contains `custom_connector_arn_prefix`, `connector_id`, and the Scheduler-provided `scheduled_time`.

## Recovery

Replay only after the underlying permission, throttling, or code failure is corrected. Before replaying a start-orchestration or terminal Batch event, inspect the persisted job-definition ARN and confirm its AWS Batch state is `ACTIVE` or `INACTIVE`. Preserve the Scheduler payload unchanged when replaying it. Scheduler delivery is at least once; CCF normalizes `scheduled_time` into the start request's idempotency key, so retries for the same scheduled occurrence resolve to the same job. Changing or omitting `scheduled_time` is rejected and must not be used to force a replay. Verify the persisted connector and job state before replaying any Scheduler or jobs-stream message. Terminal reconciliation remains idempotent, but operators must still verify the persisted Batch and CCF job states before replaying its DLQ message.

## Escalation

Escalate P1 alarms to the CCF on-call owner immediately. Escalate P2 alarms if two evaluation periods breach or if any customer connector is blocked. Include the [Amazon CloudWatch](https://aws.amazon.com/cloudwatch/) alarm ARN, Lambda request IDs, affected connector and job identifiers, and the change or incident ticket. Do not include connector configuration, source credentials, or document content.
