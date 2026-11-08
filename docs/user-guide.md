# User guide

This guide is for two readers. One calls the Custom Connector Framework (CCF)
API to manage destinations, custom connectors, and jobs. The other writes a
connector implementation for a new source. The
[glossary](../DESIGN.md#glossary) defines the terms. Deploying and operating the framework is in
the [administrator guide](./administrator-guide.md).

## How a connector reaches your account

A connector implementation runs as a job in the service account, but it acts
in your account only through roles you own. These roles take part:

| Role | Account | Owner | Used by | For |
| --- | --- | --- | --- | --- |
| Batch execution role (`role/ccf/CcfBatchExecutionRole`) | framework | framework | AWS Batch and Fargate, before the containers start | Pull the connector image from the tenant's repository, write the log router's own diagnostics. Its credentials never reach the containers |
| Runtime role (one per custom connector) | framework | framework | The running containers (connector and log router), as their task role | Call the framework API (SigV4 plus the delegation check), assume the source-access role and, when set, the log-delivery role. No S3 or other permission |
| Source-access role | tenant | tenant | The runtime role, by `AssumeRole` with the external ID | Read `secret_arn`; write, read, list, and delete documents under the custom connector's prefix in the destination bucket; anything else in the tenant account the connector implementation needs |
| Log-delivery role (optional) | tenant | tenant | The log router, as the runtime role, by `AssumeRole` with the external ID | `logs:CreateLogStream` and `logs:PutLogEvents` on the log destination's log group |
| Destination role (`CcfDestinationRole-<name>`, one per destination) | tenant | tenant | The destination workflow, the connector deletion workflow, the API Lambda, and the ingestion consumer | Create and delete destination infrastructure, delete documents' files, start ingestion. Never used by the container |

The container runs as the runtime role and acts as the tenant only through a
session of its source-access role. The toolkit uses that one session for the secret,
the destination bucket, and the start-of-job sweep, and hands it to your
implementation as `JobContext.source_session`.

## Call the API

The API takes requests signed with AWS Signature Version 4 for the
`execute-api` service, at the `ApiEndpoint` output of the
`CustomConnectorFramework` stack. Paths start with `/api/v1/destinations`. The
toolkit's `CcfClient` signs each call with a boto3 session's credentials, fills
in `client_token` when you leave it out, and retries throttled calls and calls
that are safe to repeat:

```python
import boto3
from ccf_api_models.models import ListDestinationsRequest
from custom_connector_framework import CcfClientSettings, create_ccf_client

client = create_ccf_client(
    CcfClientSettings(
        endpoint="https://abcdef1234.execute-api.us-east-1.amazonaws.com/ccf/",
        region="us-east-1",
        session=boto3.Session(),
    )
)
for destination in client.list_destinations(ListDestinationsRequest()).destinations:
    print(destination.destination_id, destination.name, destination.status)
```

Each client method takes the operation's request model from
`ccf_api_models.models` and returns its response model. An error raises
`CcfApiError`, whose `code` is one of the error codes below.

### Callers

A tenant principal is any IAM principal in the service account or in an
account the framework's `allowedTenantAccountIds` lists. A runtime role is the
custom connector's own job, calling through the toolkit. The API refuses every
other caller with `UnauthorizedException`.

| Operation | Tenant principal | Runtime role |
| --- | --- | --- |
| `CreateDestination`, `ListDestinations`, `UpdateDestination`, `DeleteDestination` | yes | no |
| `GetDestination` | yes | yes |
| `CreateCustomConnector`, `ListCustomConnectors`, `UpdateCustomConnector`, `DeleteCustomConnector` | yes | no |
| `GetCustomConnector` | yes | yes |
| `StartCustomConnectorJob`, `ListCustomConnectorJobs`, `StopCustomConnectorJob` | yes | no |
| `GetCustomConnectorJob` | yes | yes |
| `BatchPutCustomConnectorDocuments`, `BatchDeleteCustomConnectorDocuments`, `BatchDeleteCustomConnectorCollections` | no | yes |
| `ListCustomConnectorDocuments`, `ListCustomConnectorCollections` | yes | yes |
| `PutCustomConnectorCheckpoint`, `GetCustomConnectorCheckpoint` | yes | yes |
| `DeleteCustomConnectorCheckpoint` | yes | no |

### Errors

Every error response carries `code`, `message`, and `request_id`. The message
never holds a configuration value or internal detail.

| Exception | HTTP | `code` | When |
| --- | --- | --- | --- |
| `BadRequestException` | 400 | `BAD_REQUEST` | A request member fails validation. The API's log names the member; the response doesn't. |
| `UnauthorizedException` | 403 | `UNAUTHORIZED` | The caller table doesn't allow the caller, or the tenant account isn't allowed. |
| `ResourceNotFoundException` | 404 | `RESOURCE_NOT_FOUND` | A resource in the path doesn't exist or belongs to another tenant or parent, or no checkpoint is stored. |
| `ConflictException` | 409 | `CONFLICT` | A status check failed, a `client_token` was reused with another request, or a collection still has documents. |
| `ThrottlingException` | 429 | `THROTTLED` | The API or a service behind it throttled the call. Retry with backoff. |
| `ServiceUnavailableException` | 503 | `SERVICE_UNAVAILABLE` | A service behind the API failed. |
| `InternalServerError` | 500 | `INTERNAL_SERVER_ERROR` | Anything else. |

## Destinations

A destination is where a custom connector's documents go: a bucket in your
account and, for type `BEDROCK`, a knowledge base and data source that read
it. You create it with `CreateDestination`, naming the destination's role, and
the framework builds it. Normally the framework's CDK app creates the role and
the destination in a `CustomConnectorDestinationFor<Name>` stack instead, as the
[administrator guide](./administrator-guide.md#deploy-a-destination) shows.

| Type | What the framework creates | After a successful job |
| --- | --- | --- |
| `BEDROCK` | A bucket, a Bedrock knowledge base, and a data source over the bucket | Starts the knowledge base's ingestion |
| `QUICK` | A bucket with Amazon Quick metadata files | Nothing. You connect Amazon Quick to the bucket. |
| `S3` | A bucket with plain metadata files | Nothing |

A new destination starts in `CREATING_BUCKET`, moves through
`CREATING_KNOWLEDGE_BASE` and `CREATING_DATA_SOURCE` for `BEDROCK`, and ends
`ACTIVE` or `FAILED`. A `FAILED` destination's `status_reason` has the form
`<error>: <cause> (correlation <id>)`. When a reason is longer than 2,048
characters, the middle of its cause is cut. Its correlation ID, of up to 114
characters, is kept. `DeleteDestination` returns
`ConflictException` while the destination is being created or still has
custom connectors. Otherwise it moves the destination to `DELETING` and deletes
the bucket, the knowledge base, and the data source.

`access_control_enabled` turns on access control lists, and only a `BEDROCK`
destination accepts `true`. See [Access control lists](#access-control-lists).

`metadata_fields` declares the document attributes a `BEDROCK` destination's
knowledge base knows about. It holds up to 30 fields, each with a `name`, a
`type` (`STRING` or `STRING_LIST`), and a `searchability` (`ENABLED` or
`DISABLED`), Bedrock's setting for whether the field is searchable. A name is
the attribute's name in the documents' metadata files, such as `owner` or
`repository` for GitHub documents, and can't start with `_`, which Bedrock
reserves. You can filter a search on any attribute a document has, whether or
not it's declared here. A new destination has no fields. `UpdateDestination`
sets them on the knowledge base, then stores and returns them, and an empty
list removes every field. The destination stays `ACTIVE`, so jobs keep
running. A field's type can't change, so remove the field in one update and
add it back with the new type in the next.

| Operation | Request | Response |
| --- | --- | --- |
| `CreateDestination`, `POST /destinations` | `name`, `description?`, `type` (`BEDROCK`, `QUICK`, `S3`), `role_arn` (`arn:<partition>:iam::<your account>:role/ccf/destinations/CcfDestinationRole-<name>`), `access_control_enabled?` (default `false`), `client_token?` | 201, `destination`: `destination_id`, `arn`, `name`, `description?`, `type`, `access_control_enabled`, `status`, `created_at`, `updated_at` |
| `GetDestination`, `GET /destinations/{destination_id}` | `destination_id`; header `x-ccf-connector-id?`, which only a runtime role sends | 200, `destination`: the fields above plus `status_reason?`, `role_arn`, `metadata_fields`, and `destination_resources` (`bucket_arn?`, `bucket_name?`, `knowledge_base_arn?`, `knowledge_base_id?`, `data_source_id?`, `knowledge_base_role_arn?`, each set once its resource exists) |
| `ListDestinations`, `GET /destinations` | `name?`, `max_results?` (1 to 100), `next_token?` | 200, `destinations` (each `destination_id`, `arn`, `name`, `type`, `status`, `created_at`, `updated_at`), `next_token?` |
| `UpdateDestination`, `PUT /destinations/{destination_id}` | `destination_id`, `metadata_fields` | 200, `destination`, as `GetDestination` returns it. `BadRequestException` for an `S3` or `QUICK` destination, or fields Bedrock rejects: a name sent twice, or a field whose type changes. `ConflictException` while the destination isn't `ACTIVE`, or for about 5 seconds after an earlier update while the knowledge base applies it. `UnauthorizedException` when the destination role lacks `bedrock:UpdateKnowledgeBase`; deploy its destination stack again |
| `DeleteDestination`, `DELETE /destinations/{destination_id}` | `destination_id` | 202, no body |

## Custom connectors

A custom connector is your connector implementation registered against one
destination. It holds the image and command, a configuration string, a secret,
a schedule, the source-access role it uses in your account, and an optional
log destination. A connector project normally registers it with the
`CustomConnector` construct, through `CustomConnectorProject.addCustomConnector`,
which also creates its roles and log group. Every resource the request names,
the image, roles, secret, and log group, must be in your account and the
service Region.

- `container_properties.image_uri` must be an image in an ECR repository
  named `ccf-connectors/<name>`, tagged or pinned by digest.
  `resource_requirements` takes `cpu` (at least 0.5) and `memory` in MiB (at
  least 1,024); the log router takes 0.25 vCPU and 512 MiB of it. `timeout` is
  in seconds, 60 to 604,800.
- `connector_configuration` is a string of at most 65,536 UTF-8 bytes. The
  framework never parses it: your implementation reads it as
  `JobContext.connector_configuration`.
- `source_access.role_arn` names your source-access role, `CcfSourceRole-<name>`.
  `secret_arn` names an AWS Secrets Manager secret, which the toolkit reads
  through that role. The response adds `external_id`, which the role's trust
  policy must require.
- `log_destination` (`type` `CLOUDWATCH_LOGS`, `arn` of a log group,
  `role_arn` of a log-delivery role named `CcfLogDeliveryRole-<name>`) keeps
  what the container prints. Without it, nothing the container prints is
  kept. With it, the job's log router writes every line to the stream named
  after the job ID.
- `schedule` (`expression`, an EventBridge Scheduler expression such as
  `cron(0 2 ? * SUN *)`; `state` `ENABLED` or `DISABLED`; `timezone?`) starts a
  job on that schedule with no job configuration.

`CreateCustomConnector` returns `runtime_role_arn`, the role your source-access
and log-delivery roles must trust. `UpdateCustomConnector` replaces only the
fields you send. Send `log_destination` or `schedule` as `null` to remove it; a
field you leave out is kept. It returns `ConflictException` while a job runs or the custom
connector is being deleted. `DeleteCustomConnector` returns `ConflictException`
while a job runs. Otherwise it deletes the schedule, every file and record, and
the runtime role.

| Operation | Request | Response |
| --- | --- | --- |
| `CreateCustomConnector`, `POST .../custom-connectors` | `destination_id`, `name`, `description?`, `container_properties` (`image_uri`, `command`, `resource_requirements` {`cpu`, `memory`}, `timeout`), `connector_configuration?`, `source_access` (`role_arn`, `secret_arn?`), `log_destination?` (`type`, `arn`, `role_arn`), `schedule?` (`expression`, `state`, `timezone?`), `client_token?` | 201, `connector`: `connector_id`, `arn`, `destination_id`, `name`, `description?`, `status`, `created_at`, `updated_at`, `runtime_role_arn` |
| `GetCustomConnector`, `GET .../custom-connectors/{connector_id}` | `destination_id`, `connector_id` | 200, `connector`: the fields above plus `container_properties`, `connector_configuration?`, `source_access` (`role_arn`, `secret_arn?`, `external_id`), `log_destination?`, `schedule?` |
| `ListCustomConnectors`, `GET .../custom-connectors` | `destination_id`, `name?`, `max_results?`, `next_token?` | 200, `connectors` (each `connector_id`, `arn`, `destination_id`, `name`, `description?`, `status`, `created_at`, `updated_at`), `next_token?` |
| `UpdateCustomConnector`, `PUT .../custom-connectors/{connector_id}` | `destination_id`, `connector_id`, `name?`, `description?`, `container_properties?` (each member optional; `cpu` and `memory` together), `connector_configuration?`, `source_access?`, `log_destination?` (null removes it), `schedule?` (null deletes it) | 200, `connector`: the summary fields |
| `DeleteCustomConnector`, `DELETE .../custom-connectors/{connector_id}` | `destination_id`, `connector_id` | 202, no body |

A custom connector's `status` is `AVAILABLE`, `IN_USE` while a job runs, or
`DELETING`.

## Jobs

A job is one run of a custom connector. A custom connector runs one job at a
time: `StartCustomConnectorJob` returns `ConflictException` while one runs. A
job moves from `STARTED` to `RUNNING` and ends `SUCCEEDED`, `FAILED`, or, after
`StopCustomConnectorJob`, `STOPPED`. A failed job's `status_reason` says why,
and names the log group and stream when the custom connector has a log
destination; the
[administrator guide](./administrator-guide.md#troubleshoot-a-failed-job) maps
each reason to its cause.

Each job runs in a `CcfJobExecution` Step Functions execution. When that
execution ends without recording the job's end, the job keeps its status and
its custom connector stays `IN_USE`. Two calls recover it. The start call,
`StartCustomConnectorJob`, checks the execution of the job that holds the
custom connector. When that execution is no longer running, the call ends the
job `STOPPED` and starts the new one. A call to `StopCustomConnectorJob` on
the job also ends it `STOPPED` and releases its custom connector.

| Operation | Request | Response |
| --- | --- | --- |
| `StartCustomConnectorJob`, `POST .../jobs` | `destination_id`, `connector_id`, `job_configuration?` (at most 65,536 bytes, never parsed), `client_token?` | 201, `job`: `job_id`, `arn`, `status` (`STARTED`), `created_at`, `updated_at` |
| `GetCustomConnectorJob`, `GET .../jobs/{job_id}` | `destination_id`, `connector_id`, `job_id` | 200, `job`: the fields above plus `status_reason?`, `job_configuration?`, `service_execution?` (`execution_id`, `execution_role_arn`, `job_role_arn`, `started_at?`, `completed_at?`) |
| `ListCustomConnectorJobs`, `GET .../jobs` | `destination_id`, `connector_id`, `status?`, `max_results?`, `next_token?` | 200, `jobs` (summaries, newest first), `next_token?` |
| `StopCustomConnectorJob`, `POST .../jobs/{job_id}/stop` | `destination_id`, `connector_id`, `job_id` | 202, no body |

## Collections, documents, and checkpoints

The toolkit calls these operations for your implementation. You call the list
operations and the checkpoint operations yourself to inspect a custom connector
or to change its checkpoints between jobs. After
`DeleteCustomConnectorCheckpoint`, the next job gets no checkpoint for that
collection, so the implementation starts that collection over.

| Operation | Request | Response |
| --- | --- | --- |
| `BatchPutCustomConnectorDocuments`, `POST .../documents/batch-put` | `destination_id`, `connector_id`, `collection_id?`, `source_collection_id?`, `documents` (1 to 99, each `document_id`, `source_document_id`, `content_hash`, `source_version?`) | 204, no body |
| `BatchDeleteCustomConnectorDocuments`, `POST .../documents/batch-delete` | `destination_id`, `connector_id`, `collection_id?`, `document_ids` (1 to 99) | 204, no body |
| `ListCustomConnectorDocuments`, `GET .../documents` | `destination_id`, `connector_id`, `collection_id?`, `max_results?`, `next_token?` | 200, `documents` (each `collection_id`, `document_id`, `source_document_id`, `content_hash`, `source_version?`, `created_at`, `updated_at`), `next_token?` |
| `ListCustomConnectorCollections`, `GET .../collections` | `destination_id`, `connector_id`, `max_results?`, `next_token?` | 200, `collections` (each `collection_id`, `source_collection_id?`, `checkpoint_data?`, `created_at`, `updated_at`), `next_token?` |
| `BatchDeleteCustomConnectorCollections`, `POST .../collections/batch-delete` | `destination_id`, `connector_id`, `collections` (1 to 100, each `collection_id`) | 204, no body |
| `PutCustomConnectorCheckpoint`, `PUT .../checkpoint` | `destination_id`, `connector_id`, `collection_id?`, `source_collection_id?`, `checkpoint_data` (2 to 16,384 bytes) | 204, no body |
| `GetCustomConnectorCheckpoint`, `GET .../checkpoint` | `destination_id`, `connector_id`, `collection_id?` | 200, `checkpoint`: `checkpoint_data`, `created_at`, `updated_at` |
| `DeleteCustomConnectorCheckpoint`, `DELETE .../checkpoint` | `destination_id`, `connector_id`, `collection_id?` | 204, no body |

A `collection_id` left out means `col-default`. Any other `collection_id` is
`col-` plus the first 16 hex characters of the SHA-256 of its
`source_collection_id`, which the request must also send. A `document_id` is
`doc-` plus the first 16 hex characters of the SHA-256 of its
`source_document_id`. A collection that still has documents can't be deleted.

## Write a connector implementation

A connector implementation is a Python class that subclasses
`ConnectorImplementation` and overrides `run()`. The toolkit builds it once per
job with `self.job`, a `JobContext`, and `self.store`, a `DocumentStore`, and
calls `run()` once. Your code reads the source and hands documents, deletes,
and checkpoints to the store; the toolkit makes every framework call.

`JobContext` holds:

| Field | What it holds |
| --- | --- |
| `destination_id`, `connector_id`, `job_id` | The job's IDs |
| `connector_configuration` | The custom connector's configuration string as a `SecretStr`, or `None` |
| `job_configuration` | The string given to `StartCustomConnectorJob` as a `SecretStr`, or `None` |
| `secret` | The value of the custom connector's secret as a `SecretStr`, or `None` |
| `source_session` | A boto3 session with the source-access role's credentials, refreshed before they expire |

Read checkpoints through the store's `get_checkpoint` or `collections()`.

When `run()` returns, the toolkit sends every queued write and the job
succeeds. When it raises, the toolkit sends the queued puts, then the deletes,
logs the exception with its stack trace, and the job fails with
`ConnectorImplementationError`.

### Minimal implementation example

This implementation reads notes from its own configuration, so it runs with no
source. It writes each note as a document with an attribute, and deletes the
stored documents whose notes are gone. The toolkit's test suite runs this
exact code.

<!-- user-guide-example -->

```python
import io
import json
import logging

from custom_connector_framework import (
    ConnectorImplementation,
    DeleteDocumentRequest,
    DiffRequest,
    DocumentAttribute,
    PutDocumentRequest,
    SourceDocument,
    StringAttribute,
)

logger = logging.getLogger(__name__)


class NotesConnector(ConnectorImplementation):
    """Reads {"notes": [{"id", "title", "author", "text"}]} from the connector configuration."""

    def run(self) -> None:
        if self.job.connector_configuration is None:
            raise ValueError("NotesConnector needs a connector configuration")
        notes = json.loads(self.job.connector_configuration.get_secret_value())["notes"]
        for note in notes:
            self.store.put(
                PutDocumentRequest(
                    document_id=note["id"],
                    file_name=f"{note['id']}.md",
                    content=io.BytesIO(note["text"].encode("utf-8")),
                    title=note["title"],
                    attributes=(DocumentAttribute(name="author", value=StringAttribute(value=note["author"])),),
                )
            )
        current = tuple(SourceDocument(document_id=note["id"]) for note in notes)
        for document_id in self.store.diff(DiffRequest(current=current)).missing:
            self.store.delete(DeleteDocumentRequest(document_id=document_id))
        logger.info("notes_read", extra={"notes": len(notes)})
```

Package it in a container image for linux/arm64 whose command is
`ccf-connector run <module>:<class>`, for example
`ccf-connector run notes_connector:NotesConnector`. The GitHub connector's
[Dockerfile](../connectors/github/backend/Dockerfile) shows one way to build it
with uv.

### Document store

Every store request names documents and collections by your source's own IDs,
and the store turns them into `doc-` and `col-` IDs. A `collection_id` of
`None` means the default collection.

| Method | What it does |
| --- | --- |
| `put(PutDocumentRequest)` | Writes one document. Members: `document_id`, `file_name`, `content` (a binary stream), `attributes` (a tuple of `DocumentAttribute(name, value)`, each value a `StringAttribute`, `NumberAttribute`, or `StringListAttribute`, with unique names), `collection_id`, `source_version`, `media_type`, `access_control`, `title`, `source_uri`. |
| `delete(DeleteDocumentRequest)` | Queues a delete of `document_id` in `collection_id`. A document that isn't stored is ignored. |
| `documents(ListDocumentsRequest)` | Yields each stored document of a collection: `document_id`, `source_version`, `updated_at`. |
| `diff(DiffRequest)` | Compares `current`, the `SourceDocument`s (`document_id`, `source_version`) a collection has now, with what is stored, and returns `new`, `changed`, and `missing` IDs. |
| `collections()` | Yields each stored collection: `collection_id` (`None` for the default) and `checkpoint`. |
| `get_checkpoint(GetCheckpointRequest)` | Returns a collection's checkpoint, or `None`. |
| `save_checkpoint(SaveCheckpointRequest)` | Saves a collection's `checkpoint`, at most 16 KB, after sending that collection's queued puts and deletes. |
| `delete_collection(DeleteCollectionRequest)` | Deletes a collection and every document in it. |
| `get_report()` | Returns a `StoreReport`: written, unchanged, deleted, skipped, and pending counts per collection and in total. |

`put` reads the first 8 KB to detect the media type unless you pass
`media_type`. It skips empty text and content the destination can't index, and
writes the metadata file, then the file. When the stored file already has the same content hash, it writes nothing and
only refreshes the record, which the store sends in batches of up to 99 per
collection.

### Access control lists

Set `PutDocumentRequest.access_control` to an `AccessControl` with
`AccessControlEntry` items (`principal_id`, `principal_type` `USER` or `GROUP`,
`access` `ALLOW` or `DENY`). A `USER` principal is the user's email address,
which both Bedrock and Amazon Quick match exactly. What the store does with the
list depends on the destination:

| Destination | What the store does |
| --- | --- |
| `BEDROCK` with `access_control_enabled` `true` | Writes the list into the metadata file in Bedrock's format. A `GROUP` entry raises `UnsupportedAccessControlError`. Bedrock ingests no document without an access list, and a search without a user returns nothing. |
| `BEDROCK` without access control lists | Leaves the list out, and logs one WARNING per flush with the number of documents that had one. |
| `QUICK` | Writes the list whenever a document has one. You turn on access control lists when you connect Amazon Quick to the bucket. |
| `S3` | Writes no list. |

### Checkpoints

A checkpoint is a string your implementation keeps for one collection, often
JSON. The toolkit stores it and never reads it. `get_checkpoint` parses a
checkpoint into the Pydantic model you name. It returns `None` when nothing is
stored or the text doesn't parse. `collections()` lists every stored collection
with its checkpoint, so one call reads them all.

Consider a Confluence-like source, where each space is a collection and the
source can list pages changed since a time. Reading only changes is fast but
never notices a deleted page, so the implementation returns to a full read
every 7 days:

```python
from datetime import UTC, datetime, timedelta

from pydantic import AwareDatetime, BaseModel

from custom_connector_framework import (
    ConnectorImplementation,
    DeleteDocumentRequest,
    DiffRequest,
    GetCheckpointRequest,
    SaveCheckpointRequest,
    SourceDocument,
)

FULL_READ_INTERVAL = timedelta(days=7)


class FullRead(BaseModel):
    last_full_read: AwareDatetime


class SpaceRead(BaseModel):
    changed_since: AwareDatetime


class WikiConnector(ConnectorImplementation):
    def run(self) -> None:
        now = datetime.now(UTC)
        last = self.store.get_checkpoint(GetCheckpointRequest(checkpoint_type=FullRead))
        full_read = last is None or now - last.last_full_read > FULL_READ_INTERVAL
        wiki = connect_to_wiki(self.job)  # your source client
        for space in wiki.spaces():
            stored = self.store.get_checkpoint(GetCheckpointRequest(checkpoint_type=SpaceRead, collection_id=space.key))
            since = None if full_read or stored is None else stored.changed_since
            pages = wiki.pages(space.key, changed_since=since)
            for page in pages:
                self.store.put(page.to_put_request(collection_id=space.key))
            if since is None:
                current = tuple(SourceDocument(document_id=page.id) for page in pages)
                for page_id in self.store.diff(DiffRequest(current=current, collection_id=space.key)).missing:
                    self.store.delete(DeleteDocumentRequest(document_id=page_id, collection_id=space.key))
            checkpoint = SpaceRead(changed_since=now).model_dump_json()
            self.store.save_checkpoint(SaveCheckpointRequest(checkpoint=checkpoint, collection_id=space.key))
        if full_read:
            self.store.save_checkpoint(SaveCheckpointRequest(checkpoint=FullRead(last_full_read=now).model_dump_json()))
```

`connect_to_wiki`, `spaces`, `pages`, and `to_put_request` stand for your
source's own client. Saving a space's checkpoint first sends that space's
queued writes. A job that fails partway keeps the spaces it finished, and the
next job resumes from their checkpoints. A space that no longer exists
needs `delete_collection`, which the GitHub connector does for repositories
that drop out.

### Logging

Log with the standard `logging` module, or `print`. The toolkit writes each
log record as one JSON line on stdout, with `timestamp`, `level`, `logger`,
`message`, `job_id`, `connector_id`, and `service`, and every field you pass in
`extra=`. The toolkit publishes no metrics, so a log event with a fixed
`message` and its counts as fields is how a job reports what it did, as the
GitHub connector does.

Output reaches your account only when the custom connector has a log
destination. Then the log router writes each line to your log group. Without a
log destination, it is discarded. Nothing the container prints reaches the
service account.

### Read other resources in your account

Use `self.job.source_session` for every AWS call your implementation makes. It
holds your source-access role's credentials and is the same session the toolkit
writes documents with. Your code acts in your account with exactly the
permissions you grant that role. Grant them on the `CustomConnector`:

```typescript
sourceBucket.grantRead(connector);
```

The construct is the role's grant principal, so the grant lands on the role.

A job reaches S3 in the service Region through the job VPC's S3 gateway
endpoint. Its policy allows destination buckets and every bucket the framework
account doesn't own. So your implementation can read, through
`source_session`, source buckets in any account other than the framework
account. In the service Region, a job can't reach a service-account
bucket other than a destination bucket. When your tenant account is the
service account, as in the walkthrough, that includes your own buckets, so
your source bucket must be in another Region or another account.

Buckets in other Regions, in any account, are reached through the NAT gateway,
where the endpoint policy doesn't apply. Service-account buckets there are
protected by their own bucket policies and by the runtime role having no S3
permission. A job reads only what you grant `CcfSourceRole-<name>`.

### Test it

The `custom_connector_framework.testing` module runs your implementation
against in-memory fakes of the framework API and S3, with no AWS account:

```python
import json

from custom_connector_framework.testing import (
    RunImplementationRequest,
    SeedDocument,
    SeedState,
    run_implementation,
)

run = run_implementation(
    RunImplementationRequest(
        implementation=NotesConnector,
        connector_configuration=json.dumps({"notes": [{"id": "n1", "title": "One", "author": "ana", "text": "Hi"}]}),
        seed=SeedState(documents=(SeedDocument(document_id="gone"),)),
    )
)
assert run.report.total.written == 1
assert run.report.total.deleted == 1
```

Besides the implementation, `RunImplementationRequest` takes
`job_configuration`, `secret`, seeded collections and checkpoints,
`destination_type`, `access_control_enabled`, and the job's IDs. The returned
`TestRun` holds the job's `exit_code`, the `report`, the stored `documents`
and `collections`, the bucket's `files`, the framework operations the run
called (`api_calls`), and the JSON `log_events`.

## Deploy your connector

A connector deploys after its destination. The framework's CDK app creates a
destination in its own `CustomConnectorDestinationFor<Name>` stack (see the
[administrator guide](./administrator-guide.md)), which outputs the
`DestinationId`. The GitHub connector's
[`GitHubCustomConnector`](../connectors/github/cdk/lib/stacks/github-custom-connector-stack.ts)
stack is the template a new connector copies. It holds one
`CustomConnectorProject`, which builds the image into its own
`ccf-connectors/<name>` repository, and one custom connector per source the
project reads:

```typescript
import { App, Duration, RemovalPolicy, Stack } from "aws-cdk-lib";
import { RetentionDays } from "aws-cdk-lib/aws-logs";
import { CustomConnectorProject, Destination, FrameworkValues } from "custom-connector-framework-cdk";

const app = new App();
const stack = new Stack(app, "WikiCustomConnector");
// With no literals, the framework's values come from the CustomConnectorFramework stack's exports.
const framework = FrameworkValues.fromConfig();
const project = new CustomConnectorProject(stack, "Project", {
  framework,
  destination: Destination.fromDestinationId(stack, "Destination", { destinationId: "dst-7c41e9a2b5d0", framework }),
  image: {
    directory: "../..",
    file: "connectors/wiki/backend/Dockerfile",
    repositoryName: "ccf-connectors/wiki",
    maxImageAge: Duration.days(90),
  },
  removalPolicy: RemovalPolicy.RETAIN,
  logRetention: RetentionDays.ONE_MONTH,
});
project.addCustomConnector({
  id: "Engineering",
  name: "wiki-engineering",
  command: ["wiki_connector:WikiConnector"],
  compute: { cpu: 1, memoryMib: 4096, timeout: Duration.hours(2) },
  sourceAccessServices: [],
  deletionTimeout: Duration.hours(2),
});
```

The image's entry point is `ccf-connector run`, so `command` names only the
implementation class. A custom connector's `name` must be unique among the
custom connectors in its account, because its role and log group names come
from it. A stack in another account passes the framework's values as
literals to `FrameworkValues.fromConfig`.

## Clients without the toolkit

You can write documents without the toolkit, for example from a language other
than Python. Your job still runs as the runtime role, assumes the source-access
role with the external ID, and calls the API with the delegation headers
`x-ccf-tenant-arn-prefix` and, for `GetDestination`, `x-ccf-connector-id`.

### Key layout

Each document's file and metadata file sit under a prefix labeled with the IDs
from its document ARN:

```text
connector_id=<connector ID>/collection_id=<collection ID>/document_id=<document ID>/<file name>
connector_id=<connector ID>/collection_id=<collection ID>/document_id=<document ID>/<file name>.metadata.json
```

For example,
`connector_id=cc-e020d65d76c0/collection_id=col-3f9a1c2b5d7e9f01/document_id=doc-8d41e07a9c3f2b6e/README.md`.
Your source-access role reaches only `connector_id=<connector ID>/`. For a key
that doesn't exist, S3 returns 403, not 404, so list a document's prefix before
you read a key in it.

### Write order

The backend keeps its own records consistent, but the files in the bucket are
your responsibility. Follow these rules:

1. Upload each file and its metadata file to their final keys, then record them
   with `BatchPutCustomConnectorDocuments`.
1. Record or delete every file you upload. An unrecorded file stays searchable
   until the next toolkit-based job's sweep deletes it.
1. Send a collection's records before you save its checkpoint with
   `PutCustomConnectorCheckpoint`, so the checkpoint never covers documents
   that aren't recorded.

`BatchDeleteCustomConnectorDocuments` deletes a document's files as well as its
record, so you don't delete them yourself. A client that gets these rules wrong
affects only its own custom connector's documents.

### Register without `CustomConnector`

A custom connector registered with `CreateCustomConnector` directly needs the
same tenant resources the construct creates:

1. Call `CreateCustomConnector` with `source_access.role_arn` naming a role
   `CcfSourceRole-<name>` and, for logs, `log_destination` naming a log group
   and a role `CcfLogDeliveryRole-<name>`. The roles don't have to exist yet.
1. Create `CcfSourceRole-<name>` with the
   [source-access role's trust policy and permissions](./administrator-guide.md#source-access-role),
   using the `runtime_role_arn` and `external_id` the API returns.
1. For logs, create the log group and `CcfLogDeliveryRole-<name>` with the
   [log-delivery role's policies](./administrator-guide.md#log-delivery-role).

A job that starts before the roles exist fails when it assumes
`CcfSourceRole-<name>`, and the next one succeeds.
