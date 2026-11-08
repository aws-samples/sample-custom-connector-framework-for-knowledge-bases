---
description: Write a connector implementation in Python with the Custom Connector Framework toolkit, package it as a container image, and test it locally.
---

# Write a connector implementation

A connector implementation is the code that reads your source. It's a Python class that subclasses `ConnectorImplementation` from the toolkit, the framework's `custom_connector_framework` Python package, and overrides `run()`. Your code hands documents, deletes, and checkpoints to the toolkit's document store. The toolkit makes every framework call, writes every file, and reports the job's result.

## How the toolkit runs your code

For each job, the toolkit in the container:

1. Reads the job, the custom connector, and the destination from the framework API.
1. Assumes the custom connector's source-access role in your account, and reads its secret.
1. Deletes any file under the custom connector's prefix that no document record names, such as a file an interrupted job wrote before it sent the record.
1. Builds your class once, with `self.job`, a `JobContext`, and `self.store`, a `DocumentStore`, and calls `run()` once.
1. When `run()` returns, sends every queued write, and the job succeeds.
1. When `run()` raises, logs the exception with its stack trace, sends the queued puts and then the deletes, and exits 70, so the job fails with `ConnectorImplementationError`. The work done before the failure is kept.

`JobContext` holds:

| Member | What it holds |
| --- | --- |
| `destination_id`, `connector_id`, `job_id` | The job's IDs |
| `connector_configuration` | The custom connector's configuration string as a `SecretStr`, or `None` |
| `job_configuration` | The string given to `StartCustomConnectorJob` as a `SecretStr`, or `None` |
| `secret` | The value of the custom connector's secret as a `SecretStr`, or `None` |
| `source_session` | A boto3 session with the source-access role's credentials, refreshed before they expire |

The framework never parses either configuration string, so your implementation owns its format. JSON is a common choice.

## Minimal example

This implementation reads notes from its own configuration, so it runs with no source. It writes each note as a document with an attribute, and deletes the stored documents whose notes are gone. The toolkit's test suite runs this exact code.

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

[Store documents and checkpoints](document-store.md) covers every store method, access control lists, and checkpoints.

## Package it as a container image

Build a container image for `linux/arm64` whose command is `ccf-connector run <module>:<class>`, for example `ccf-connector run notes_connector:NotesConnector`. The image's entry point can be `ccf-connector run`, so the custom connector's `command` names only the class. The example GitHub connector's [Dockerfile](https://github.com/aws-samples/sample-custom-connector-framework-for-knowledge-bases/blob/main/connectors/github/backend/Dockerfile) shows one way to build it with uv. The CDK constructs in [Deploy a connector](deploy-a-connector.md) build and push the image for you.

## Log what a job does

Log with the standard `logging` module. The toolkit writes each log record as one JSON line on stdout, with `timestamp`, `level`, `logger`, `message`, `job_id`, `connector_id`, and `service`, plus every field you pass in `extra=`. A `print` call also reaches stdout, as a plain-text line. The toolkit publishes no metrics. To report what a job did, log an event with a fixed `message` and its counts as fields, as the GitHub connector does.

Output reaches your account only when the custom connector has a log destination. The job's log router then writes each line to your log group, in a stream named after the job ID. Without a log destination, the output is discarded. Nothing the container prints reaches the service account.

## Read other resources in your account

Use `self.job.source_session` for every AWS call your implementation makes. It holds your source-access role's credentials, and it's the same session the toolkit writes documents with. Your code acts in your account with exactly the permissions you grant that role. Grant them on the `CustomConnector` construct, which is the role's grant principal:

```typescript
sourceBucket.grantRead(connector);
```

The role's permissions boundary allows only [Amazon S3](https://aws.amazon.com/s3/), [AWS Secrets Manager](https://aws.amazon.com/secrets-manager/), [AWS KMS](https://aws.amazon.com/kms/), [Amazon CloudWatch Logs](https://aws.amazon.com/cloudwatch/), and the services the custom connector lists in `sourceAccessServices`. A grant to any other service has no effect.

A job reaches Amazon S3 in the service Region through the job VPC's S3 gateway endpoint. Its policy allows destination buckets and every bucket the service account doesn't own. So your implementation can read source buckets in your account through `source_session`. When your tenant account is the service account, as in the [tutorial](../getting-started/tutorial.md), your source bucket must be in another Region or another account. [Security](../administrator-guide/security.md#network-requirements) explains why.

## Test it locally

The `custom_connector_framework.testing` module runs your implementation against in-memory fakes of the framework API and Amazon S3, with no AWS account:

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

Besides the implementation, `RunImplementationRequest` takes `job_configuration`, `secret`, seeded collections and checkpoints, `destination_type`, `access_control_enabled`, the Region, the tenant account's ID, and the job's IDs. The returned `TestRun` holds the job's `exit_code`, the `report`, the stored `documents` and `collections`, the bucket's `files`, the framework operations the run called (`api_calls`), and the JSON `log_events`.
