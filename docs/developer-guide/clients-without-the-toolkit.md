---
description: Write documents to a Custom Connector Framework destination without the Python toolkit, and register a custom connector without the CDK construct.
---

# Clients without the toolkit

You can write documents without the toolkit, for example from a language other than Python. Your job still runs as the custom connector's runtime role. It assumes the source-access role with the external ID, and calls the API with the delegation headers `x-ccf-tenant-arn-prefix` and, for `GetDestination`, `x-ccf-connector-id`.

## Key layout

Each document's file and metadata file sit under a prefix labeled with the IDs from its document ARN:

```text
connector_id=<connector ID>/collection_id=<collection ID>/document_id=<document ID>/<file name>
connector_id=<connector ID>/collection_id=<collection ID>/document_id=<document ID>/<file name>.metadata.json
```

For example, `connector_id=cc-e020d65d76c0/collection_id=col-3f9a1c2b5d7e9f01/document_id=doc-8d41e07a9c3f2b6e/README.md`. Your source-access role reaches only `connector_id=<connector ID>/`. For a key that doesn't exist, [Amazon S3](https://aws.amazon.com/s3/) returns 403, not 404, so list a document's prefix before you read a key in it.

## Write order

The framework keeps its own records consistent, but the files in the bucket are your client's responsibility. Follow these rules:

1. Upload each file and its metadata file to their final keys, then record them with `BatchPutCustomConnectorDocuments`.
1. Record or delete every file you upload. An unrecorded file stays searchable until the next toolkit-based job's sweep deletes it.
1. Send a collection's records before you save its checkpoint with `PutCustomConnectorCheckpoint`, so the checkpoint never covers documents that aren't recorded.

`BatchDeleteCustomConnectorDocuments` deletes a document's files as well as its record, so you don't delete them yourself. A client that gets these rules wrong affects only its own custom connector's documents, because its source-access role reaches only its own prefix.

## Register without the `CustomConnector` construct

A custom connector registered with `CreateCustomConnector` directly needs the same tenant resources the construct creates:

1. Call `CreateCustomConnector` with `source_access.role_arn` naming a role `CcfSourceRole-<name>` and, for logs, `log_destination` naming a log group and a role `CcfLogDeliveryRole-<name>`. The roles don't have to exist yet.
1. Create `CcfSourceRole-<name>` with the [source-access role's trust policy and permissions](tenant-roles.md#source-access-role), using the `runtime_role_arn` and `external_id` the API returns.
1. For logs, create the log group and `CcfLogDeliveryRole-<name>` with the [log-delivery role's policies](tenant-roles.md#log-delivery-role).

A job that starts before the roles exist fails when it assumes `CcfSourceRole-<name>`, and the next one succeeds.
