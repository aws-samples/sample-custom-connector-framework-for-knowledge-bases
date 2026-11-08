---
description: The Custom Connector Framework's design decisions, each with what happens and why.
---

# Design decisions

Each decision says what happens when you take an action, then why.

## Ownership and accounts

These decisions keep each tenant's data and resources in its own account.

- **When you create a destination, the framework builds it in your account.** The workflow creates the bucket and knowledge base through the destination's role, so the tenant owns its documents and pays for its own index. Deleting the framework's stacks leaves every tenant's bucket and knowledge base in place.
- **When you deploy a tenant in another account, you give it the framework's values.** [AWS CloudFormation](https://aws.amazon.com/cloudformation/) exports don't cross accounts, so `FrameworkValues.fromConfig` takes the `endpoint`, `serviceAccount`, `batchExecutionRoleArn`, and `destinationWorkflowArn` as literals and imports no export.
- **When a destination role is assumed, the external ID is a state machine's ARN.** [AWS Step Functions](https://aws.amazon.com/step-functions/) sends the state machine's ARN as the external ID and has no field for another value.
- **When a job runs longer than an hour, its source-access session refreshes.** The runtime role is itself an assumed role, so role chaining caps each source-access session at one hour. The toolkit's session calls `AssumeRole` again before it expires.

## Documents

A job can fail partway, so these decisions keep the bucket, the records, and the checkpoints consistent when it does.

- **When a job writes a document, the toolkit writes the file first and records it second.** The toolkit puts the metadata file and the file at their final keys with the source-access role's session, then calls `BatchPutCustomConnectorDocuments`. Document bytes never pass through the API, so file size isn't bound by the request limit of [Amazon API Gateway](https://aws.amazon.com/api-gateway/). A client that skips the toolkit and gets the order wrong affects only its own custom connector's documents, because the source-access role reaches only that prefix.
- **When the store gets a document whose bytes and metadata are unchanged, it writes nothing.** It lists the document's prefix, reads the stored file's `ccf-content-hash`, and only refreshes the record. A rerun with no source changes rewrites no file.
- **When a document's file name changes, the store writes the new files, then deletes the old keys.** If a job dies between those steps, the next job's sweep keeps only the newest file and its metadata file.
- **When you save a collection's checkpoint, the store first sends that collection's queued puts and deletes.** A checkpoint never gets ahead of the documents it covers.
- **When a connector implementation raises, the toolkit still sends its queued writes.** It sends the puts, then the deletes, then exits 70, so the work done before the failure is kept.
- **When a connector adds a document attribute that searches should know about, you add it to the destination's metadata fields.** The destination owns its knowledge base's fields, so there are no merge or conflict rules across custom connectors.

## The API

The API stays small, and each connector keeps its own formats.

- **When you send a configuration value, the backend never parses it.** It checks only the size, at most 65,536 bytes for a configuration and 16,384 bytes for a checkpoint, and encrypts each value with the framework's [AWS KMS](https://aws.amazon.com/kms/) key. Each implementation owns its own configuration format.
- **When you repeat a create call with the same `client_token`, you get the first response.** Only `CreateDestination`, `CreateCustomConnector`, and `StartCustomConnectorJob` take a token, because only they create a resource with a new ID. Every other write names its resource, so repeating it has the same effect.
- **When a call names a resource of another tenant, it gets `ResourceNotFoundException`.** Destination and custom connector IDs are a prefix and 12 hex characters, short enough for a bucket name. Every call checks ownership, so knowing another tenant's ID gives no access to its resources.
- **When the backend refuses an authenticated caller, it answers 403.** Every caller is already authenticated by Signature Version 4.

## Failures and logs

Connector output stays in the tenant's account, and every failure can be retried.

- **When a job fails, its `status_reason` comes only from values the framework holds.** `CcfJobExecution` builds it from the container's exit code, [AWS Batch](https://aws.amazon.com/batch/)'s status reason, and the stored role, secret, and log destination, so no text from the container reaches the service account.
- **When a custom connector has no log destination, its output is discarded.** The log router still runs and uses Fluent Bit's discard output, so connector output doesn't reach the service account's `/aws/batch/job` log group.
- **When a job succeeds on a `BEDROCK` destination, one ingestion request waits per destination.** A burst of successful jobs starts one ingestion, and the next successful job recovers a lost message.
- **When an ingestion waits on an earlier one, the request retries for about 24 hours.** An ingestion job on the example GitHub destination ran 12.5 hours, and [Amazon Bedrock](https://aws.amazon.com/bedrock/) runs one ingestion job at a time on a data source.
- **When a deletion step fails after its retries, you call the same delete operation again.** The resource stays `DELETING`, the state machine's `ExecutionsFailed` alarm fires, and a new call starts a new execution, because the recorded one isn't running.
- **When a recorded creation failure ends the destination workflow, the execution still fails.** The failure is in `status_reason` and `DestinationCreationFailed`, and `ExecutionsFailed` counts every failed execution.

## Deployment

Each stack deploys on its own.

- **When a connector stack deploys, it builds its own image.** `ConnectorImage` creates the repository, and an [AWS CodeBuild](https://aws.amazon.com/codebuild/) project in the same stack builds the image and pushes it before any custom connector registers it.
- **When the framework's Batch execution role is replaced, you redeploy every connector stack.** IAM turns a deleted principal in the repository policy into an unusable ID.
- **When you change an alarm, the service stack isn't redeployed.** Alarms live in the separate `CustomConnectorFrameworkOps` stack.
- **When the destination workflow creates a bucket, it uses SSE-S3.** The destination role has no AWS KMS permission, so the workflow can't create a customer managed key per destination. A tenant cuts off access by deleting the destination or changing the bucket policy.
