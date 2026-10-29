# Backend

The backend owns CCF state, schedules, and connector-job execution. It tracks
connectors, jobs, documents, and checkpoints in [Amazon
DynamoDB](https://aws.amazon.com/dynamodb/), but document bytes do
not pass through it. Connector jobs write documents and optional provider
sidecars directly to the service [Amazon S3](https://aws.amazon.com/s3/)
destination through the toolkit.

The API boundary uses request, response, enum, and error models generated from
the Smithy-derived OpenAPI document. Global connector configuration and
immutable per-job configuration are validated as UTF-8 JSON, capped at 64 KiB,
and envelope-encrypted with the service customer-managed [AWS Key Management
Service (AWS KMS)](https://aws.amazon.com/kms/) key. The connector
or job ARN is the encryption context. Stored records contain ciphertext, and
configuration is decrypted only for an authorized get operation.

The service uses four [AWS Lambda](https://aws.amazon.com/lambda/) entry points:

- `api_handler` implements the modeled control-plane operations. It manages
  connectors and jobs, document state, checkpoints, and the service-owned
  [Amazon EventBridge Scheduler](https://aws.amazon.com/eventbridge/) schedule
  for each scheduled connector.
- `scheduled_start_handler` sends scheduled runs through the same job-start
  activity as manual `StartCustomConnectorJob` calls.
- `job_orchestrator_handler` reacts to the jobs table stream. A `STARTED` job
  registers and submits a connector-specific AWS Batch job: the shared
  framework execution role as `executionRoleArn` (pulls the image, writes
  logs), and a separate, service-managed runtime role scoped to that one
  connector as `jobRoleArn`. A `STOPPING` job is terminated.
- `job_status_handler` handles terminal Batch events. After a successful
  `BEDROCK` job it calls [Amazon Bedrock](https://aws.amazon.com/bedrock/)
  `StartIngestionJob`, then records the terminal
  CCF status and releases the connector. Other destination modes do not start a
  Bedrock ingestion job.

The data access objects under `common/storage/ddb/` own DynamoDB access and
conditional transitions. Connector reservations bind to the active job ID, and
same-status event delivery is idempotent, so stale terminal events cannot free a
newer run.

## Development

`make install` syncs runtime and development dependencies with uv. `make check`
runs Ruff, strict mypy, and unit tests. `make build` confirms that the handlers
import. The CDK project packages this source into Lambda functions, so run
`make requirements` after a runtime dependency change and before deployment.
