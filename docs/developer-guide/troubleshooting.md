---
description: Find why a Custom Connector Framework job or destination failed, and monitor a destination's ingestion in your account.
---

# Troubleshoot a job

A job's record says why it failed, and your account holds the logs and metrics to dig further.

## Find why a job failed

A failed job's cause is in its `status_reason`, which `GetCustomConnectorJob` returns. `ListCustomConnectorJobs` with `status` `FAILED` lists the failed jobs. The framework builds the reason from the connector container's exit code and the [AWS Batch](https://aws.amazon.com/batch/) status reason. It adds only values it already holds, such as the source-access role's ARN and the secret's ARN, so no text from your container reaches the service account.

| Exit code | `status_reason` | What to do |
| --- | --- | --- |
| 70 | `ConnectorImplementationError: <where>` | Your implementation raised. Read the stack trace in the log stream. |
| 71 | `JobTerminated: <Batch reason>; <where>` | The job was stopped, by `StopCustomConnectorJob` or by reaching its timeout. The Batch reason says which. |
| 72 | `SourceAccessDenied: AccessDenied assuming <role ARN>` | The source-access role's trust policy doesn't name the runtime role and external ID. Compare it with [Source-access role](tenant-roles.md#source-access-role). |
| 73 | `SourceAccessFailed: AssumeRole failed for <role ARN>` | [AWS STS](https://docs.aws.amazon.com/STS/latest/APIReference/welcome.html) failed for another reason, for example a role that doesn't exist yet. |
| 74 | `SecretUnavailable: GetSecretValue failed for <secret ARN>` | The secret is missing, empty, or not readable by the source-access role. |
| 76 | `FrameworkApiUnavailable: the job couldn't read its inputs from, or send its records to, the framework API` | The job couldn't reach the API at its start, in its sweep, or in its final send. Ask your administrator to check the API alarms. |
| Other, or none | The AWS Batch status reason | Read the AWS Batch reason. Common causes are running out of memory, an image pull failure, and a log router that failed to start. |

`<where>` is `see log group <name>, stream <job ID>` when the custom connector has a log destination, and `no log destination is set` otherwise. For codes 70 and 71, read that stream:

```bash
aws logs tail /ccf/connectors/<connector name> --log-stream-names <job ID>
```

Without a log destination, nothing the container printed was kept. Add one, or turn on `logDelivery` in the example GitHub connector's configuration, and run the job again.

## Other problems

| Problem | Cause and fix |
| --- | --- |
| A destination is `FAILED` | Its `status_reason` has the form `<error>: <cause> (correlation <id>)`. Fix the cause, delete the destination, and create it again. Your administrator can find the failed step by the correlation ID, as the [runbook](../administrator-guide/runbook-delivery-failures.md#destination-creation-failures) describes. |
| A job stays `STARTED` or `RUNNING`, and its custom connector stays `IN_USE` | The job's workflow ended without recording the job's end. Call `StopCustomConnectorJob` on the job, or start a new job, which ends the old one `STOPPED` first. |
| Image pulls fail after the framework was redeployed | The framework's Batch execution role was replaced, and your repository policy names the old one. Redeploy your connector stack to write the policy again. |
| A delete stays `DELETING` | A workflow step failed. Call the same delete operation again after the cause is fixed. Every step treats a resource that's already gone as done. |
| Documents are missing from search | See [Monitor your knowledge base](#monitor-your-knowledge-base). |

## Monitor your knowledge base

Each `BEDROCK` destination gives you ingestion logs, knowledge base metrics, and a list of ingestion jobs, in your account:

- The destination workflow delivers the knowledge base's ingestion logs to `/aws/vendedlogs/bedrock/knowledge-base/ccf-<destination ID>`. [Amazon Bedrock](https://aws.amazon.com/bedrock/) records each ingestion job's progress there, and each document's crawl, sync, and index status, with the error when a document fails. This [CloudWatch Logs Insights](https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/AnalyzingLogData.html) query lists the documents that failed:

    ```text
    filter event.connector_document_status.Status = "FAILED"
    | fields event.document_id, event.source_uri, event.error_message, @timestamp
    | sort @timestamp desc
    ```

- Amazon Bedrock publishes the knowledge base metrics `Invocations`, `ClientErrors`, `ServerErrors`, and `Throttles` for `Retrieve`, and `RawDataSize` after each ingestion job, in the `AWS/Bedrock/KnowledgeBases` namespace, with the `KnowledgeBaseId` dimension.
- `aws bedrock-agent list-ingestion-jobs --knowledge-base-id <id> --data-source-id <id>` lists each ingestion job with its status and statistics.

The framework starts one ingestion at a time per destination, after a successful job. A burst of successful jobs starts one ingestion, and an ingestion that waits on an earlier one is retried for about 24 hours.
