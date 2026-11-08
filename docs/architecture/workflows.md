---
description: How the Custom Connector Framework creates and deletes destinations, runs jobs, sets metadata fields, and starts ingestion.
---

# Workflows

These workflows move a resource through its life. Each one runs in the service account and acts in the tenant account only through the tenant's roles.

## Creating a destination

The destination stack, which the tenant deploys, creates the destination's role and then calls `CreateDestination`. The API saves the destination as `CREATING_BUCKET` and starts the `CcfDestinationWorkflow` [AWS Step Functions](https://aws.amazon.com/step-functions/) state machine. Every step that touches the tenant account runs with the destination role's credentials.

![A sequence diagram of destination creation. The destination stack creates the destination role and calls CreateDestination. The destination workflow assumes the role and creates the bucket, the knowledge base and its role, the data source, and the ingestion log delivery, then records the destination ACTIVE. The stack waits for ACTIVE and, when its configuration declares metadata fields, sets them.](../assets/diagrams/workflow-create-a-destination.png)
_Figure 1: Creating a `BEDROCK` destination._

The workflow:

1. Creates the bucket, blocks public access, turns on SSE-S3, tags it, and writes its bucket policy, which denies requests without TLS.
1. For `S3` and `QUICK`, sets the status to `ACTIVE` and stops. A `BEDROCK` destination goes on to the steps that follow.
1. Creates the knowledge base role, with the `CcfDestinationBoundary` boundary and a policy that reads only this bucket and publishes [Amazon Bedrock](https://aws.amazon.com/bedrock/)'s knowledge base metrics, and waits for IAM to propagate.
1. Creates the knowledge base, tagged `ccf:destination-id`, and waits until it's `ACTIVE`.
1. For an ACL-enabled destination, writes an empty global access control list file to `ccf-acl/global-acl.json`.
1. Creates the data source over the bucket, excluding `ccf-acl/`, with the configured deletion protection threshold. It turns on access control lists when `access_control_enabled` is `true`.
1. Delivers the knowledge base's ingestion logs to the log group `/aws/vendedlogs/bedrock/knowledge-base/ccf-<destination ID>` in the tenant account, through a delivery source and a delivery destination named `ccf-<destination ID>`.
1. Sets the status to `ACTIVE` with the resource IDs.

When a step fails after its retries, the workflow records `FAILED` with a `status_reason` of `<error>: <cause> (correlation <id>)` and publishes `DestinationCreationFailed`. For a quota error, it first deletes what it created.

## Setting metadata fields

Amazon Bedrock takes a knowledge base's custom metadata fields only through `UpdateKnowledgeBase`, after the knowledge base exists. So once the destination is `ACTIVE`, the destination stack calls `UpdateDestination` with the configuration's fields.

The API assumes the destination role, reads the knowledge base with `GetKnowledgeBase`, and sends its name, role, description, and configuration back to `UpdateKnowledgeBase` with the new fields. Then it stores the fields. The destination stays `ACTIVE`, and running jobs keep writing.

| Amazon Bedrock answers | The API answers | When |
| --- | --- | --- |
| `ValidationException` | `BadRequestException` | A name repeats, or a field keeps its name and changes type |
| `ConflictException` | `ConflictException` | The knowledge base is still `UPDATING` from an earlier update, which lasts a few seconds |
| `AccessDeniedException` | `UnauthorizedException` | The destination role lacks `bedrock:UpdateKnowledgeBase` because an earlier version of the destination stack created it. Redeploying the destination stack adds the permission. |

The stack's completion check calls again after a conflict, a throttle, or an unavailable service. It compares the fields by name, so a reordered list makes no call.

## Running a job

A job passes through the API or the scheduled start, the `CcfJobExecution` state machine, [AWS Batch](https://aws.amazon.com/batch/), and the toolkit in the container.

![A sequence diagram of a job. A schedule or a tenant starts the job, and the job workflow submits it to AWS Batch. The job assumes the source-access role, reads the source, writes new and changed files to the destination bucket, and records the documents and checkpoints. After a successful job on a BEDROCK destination, the framework queues one ingestion request and starts ingestion.](../assets/diagrams/workflow-run-a-job.png)
_Figure 2: Running a job, from its start to ingestion._

1. A schedule or `StartCustomConnectorJob` writes the job as `STARTED` and marks the custom connector `IN_USE`, in one transaction that requires the destination to be `ACTIVE`. It then starts the job's `CcfJobExecution` execution, named by the job ID.
1. The execution marks the job `RUNNING`, submits it to the AWS Batch job queue on [AWS Fargate](https://aws.amazon.com/fargate/), arm64, with the custom connector's job definition, and waits for it to end. The job definition has a `connector` container, which runs the registered image and command, and a `log-router` container, a Fluent Bit container that writes the connector's output to the log destination, or discards it.
1. In the container, the toolkit reads the job, the custom connector, and the destination, assumes the source-access role, reads the secret, sweeps files without records, and calls the implementation's `run()`.
1. When `run()` returns, the toolkit sends every queued record and exits 0. When it raises, the toolkit sends the queued puts, then the deletes, and exits 70.
1. When the Batch job ends, the execution records the final status and `status_reason`, and releases the custom connector. For a successful job on a `BEDROCK` destination, it sends a request to the ingestion queue. Last, it publishes the `JobsCompleted` and `JobDuration` metrics.

## Starting ingestion

The ingestion consumer reads one request at a time from `CcfIngestionQueue`, an [Amazon SQS](https://aws.amazon.com/sqs/) queue. It clears the destination's pending token, assumes the destination's role, and calls `StartIngestionJob` with the token as the client token.

- On a conflict, throttling, or server error, it restores the token and retries after 60, 120, 240, 480, then 900 seconds, for up to 100 receives, about 24 hours. A large ingestion can run for hours, and Amazon Bedrock runs one ingestion job at a time on a data source.
- On an access, not-found, or validation error, it sends the request to the dead-letter queue.

One request waits per destination. The job workflow writes a token only when none is pending, so a burst of successful jobs starts one ingestion, and the next successful job recovers a lost message.

## Deleting a destination or a custom connector

`DeleteDestination` refuses a destination that's still being created or still has custom connectors. Otherwise, it sets `DELETING` and starts the workflow's delete path, which deletes the data source and the knowledge base first. Then it deletes the ingestion log delivery with its source, destination, and log group, the knowledge base role, every object, the bucket, and the record.

`DeleteCustomConnector` refuses while a job runs. Otherwise, it sets `DELETING` and starts `CcfConnectorDeletionWorkflow`. The workflow deletes the schedule, every object under `connector_id=<connector ID>/` through the destination role, the document and collection records, and the runtime role. For `BEDROCK`, it asks for an ingestion, so the knowledge base drops the documents. Last, it deletes the record.

Every delete step treats not-found as done. When a step fails after its retries, the resource stays `DELETING` and the state machine's `ExecutionsFailed` alarm fires. Calling the same delete operation again starts a new execution.
