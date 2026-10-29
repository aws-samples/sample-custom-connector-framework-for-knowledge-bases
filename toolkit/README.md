# Toolkit

The toolkit gives connector authors one contract: strict connector and job
configuration models plus one function that returns the complete current set of
`Document` values. The framework fetches configuration, assumes the registered
source role, creates the source-account boto3 session, selects the [Amazon
S3](https://aws.amazon.com/s3/) destination
and provider sidecar format, and reconciles tracked state.

## Author contract

Configuration models must use Pydantic v2 with strict validation, immutable
instances, and unknown fields rejected. A `ConnectorDefinition` names those two
model classes and one document function:

```python
from pydantic import BaseModel, ConfigDict

from custom_connector_framework import (
    ConnectorContext,
    ConnectorDefinition,
    Document,
    DocumentMetadata,
)


class GitConnectorConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    repositories: tuple[str, ...]


class GitJobConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    include_archived: bool


def documents(
    context: ConnectorContext[GitConnectorConfiguration, GitJobConfiguration],
) -> list[Document]:
    source_client = context.source_session.client("service-name")
    return [
        Document.from_bytes(
            document_id="github_owner_repository_issue_123",
            content=source_client.read_current_bytes(),
            filename="issue-123.md",
            metadata=DocumentMetadata(title="Issue 123"),
        )
    ]


definition = ConnectorDefinition(
    connector_configuration_model=GitConnectorConfiguration,
    job_configuration_model=GitJobConfiguration,
    documents=documents,
)
```

The function receives the validated models and a boto3 `Session` for the exact
registered source role. Connectors without per-run settings can use the shared
strict `EmptyJobConfiguration` instead of declaring an empty model. The function
may return any iterable, including a generator, so an author can yield documents
while reading multiple repositories. Connector code creates focused source clients
from that session. It does not select roles, destinations, providers, object stores,
temporary storage, or state operations.

`Document.from_bytes()` accepts content already fetched into memory. During
preflight, the toolkit copies each body to private framework-owned temporary disk
and retains only its ID, checksum, filename, metadata, and spool location. After
successful preflight, the synchronizer releases its reference to the source
iterable before reading state. This includes materialized iterables such as lists.
During writes, body memory retained by the framework is bounded to at most one
rehydrated `Document` at a time. This framework-owned aggregate body-memory
guarantee does not cover references retained by connector code or its caller.
Per-document snapshot metadata and the complete tracked metadata map remain in
memory. The toolkit imposes no arbitrary document or snapshot size limit. The
author does not provide or manage paths.

## Full-snapshot behavior

The document function returns a full snapshot, not an additions list. The shared
runtime performs these steps once for every connector:

1. Exhaust the document iterable, read every selected body, validate every
   document ID and duplicate, compute every checksum, and spool bodies to private
   framework-owned temporary disk.
2. List all tracked document state, following every pagination token.
3. Rehydrate and write one new or changed body at a time, with at most one
   provider sidecar.
4. Delete exact tracked object and sidecar keys for documents absent from the snapshot.
5. Record writes and deletions in bounded control-plane batches.
6. Attempt context-managed cleanup of the temporary spool on every exit; any
   cleanup error propagates.

No CCF state read or destination mutation starts until preflight completes, so
a partial source read cannot turn a full snapshot into accidental deletes.

After preflight, destination operations and control-plane state updates are
ordered, not transactional. A destination write can succeed before its batch-put
state call, and a destination delete can succeed before its batch-delete state
call. A failure can therefore leave an idempotent revision partially applied while
tracked state remains unchanged. Retrying converges because revision keys include
the checksum, object writes overwrite the same key, and deleting a missing key is
a no-op. If a sidecar upload fails, the sink attempts to remove the new body. If
that cleanup also fails, the error reports both failures without exposing keys or
content, and the previously tracked revision is not deleted or replaced in state.

Updates compare stable IDs and persisted reconciliation checksums. The persisted checksum combines the document checksum with the selected adapter and renderer revision. Object keys still use only the document checksum, so a renderer-only change rewrites or removes the exact sidecar without changing checksum-specific content identity. The complete set of tracked document state is the default checkpoint: a successful full snapshot replaces changed entries, keeps unchanged entries, and untracks entries deleted from the source.
Changed documents write to checksum-specific object keys. After the replacement
body and sidecar succeed, the toolkit removes the exact prior object and optional
sidecar while preserving any keys reused by the replacement.

## Generic launcher

Connector images use one command:

```text
ccf-connector run module:definition
```

The launcher reads only these service-injected variables:

- `CUSTOM_CONNECTOR_FRAMEWORK_API_ENDPOINT`
- `CUSTOM_CONNECTOR_FRAMEWORK_CUSTOM_CONNECTOR_ID`
- `CUSTOM_CONNECTOR_FRAMEWORK_CUSTOM_CONNECTOR_ARN_PREFIX`
- `CUSTOM_CONNECTOR_JOB_ID`
- `AWS_REGION`

It calls typed `get_custom_connector(GetCustomConnectorRequest)` and
`get_custom_connector_job(GetCustomConnectorJobRequest)` through a client
built with that tenant ARN prefix, which the client sends as a delegation
header on every request. The control plane honors that header only when the
SigV4 caller's role identity is the exact service-managed runtime role
persisted for the connector named in the request, so a connector can only
ever act as itself.

The launcher parses each `SecretStr` JSON value with the definition's
`model_validate_json()` method. It assumes exactly
`connector.source_access.role_arn` with exactly
`connector.source_access.external_id` and keeps temporary credentials in a
boto3 `Session`; it does not put credentials in environment variables.

The S3 bucket, object prefix, and destination mode come only from
`get_custom_connector(...).service_reference.destination`. The root README's
[destination table](../README.md#high-level-design) shows what each mode
writes. Managed providers append `.txt` to unsupported suffixes, such as
`.java`, producing `.java.txt` objects. The launcher and connectors make no
Bedrock SDK calls.

## Typed control-plane client

`CcfClient` remains the only public control-plane client. Its generated typed
operations accept and return `ccf_api_models` request and response models. The
handwritten core owns endpoint validation, Region validation, [AWS Signature
Version 4 (SigV4)](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_sigv.html)
signing, and sanitized modeled errors. Connector code does not construct paths, verbs,
or dynamic operation calls.

## Development

`make install` uses uv to install runtime and development dependencies, including
`ccf-api-models` from `../api`. `make check` runs Ruff, strict mypy checks, and
the toolkit unit tests. The toolkit package has no deployable infrastructure.
