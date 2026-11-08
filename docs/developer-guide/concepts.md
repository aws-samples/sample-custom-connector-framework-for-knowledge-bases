---
description: The Custom Connector Framework's resources, destinations, custom connectors, jobs, collections, and documents, and how they relate.
---

# Concepts

The framework organizes a connector's work as destinations, custom connectors, jobs, collections, and documents. In this table, each resource belongs to the one above it, and each ID has a fixed prefix.

| Resource | What it is | ID |
| --- | --- | --- |
| Destination | Where a custom connector's documents go: a bucket in your account and, for type `BEDROCK`, a knowledge base and data source that read it | `dst-` and 12 hex characters |
| Custom connector | Your connector implementation, registered against one destination: its image, configuration, secret, schedule, roles, and log destination | `cc-` and 12 hex characters |
| Job | One run of a custom connector | `ccj-` and 12 hex characters |
| Collection | An optional group of a custom connector's documents, such as one repository's files, with an optional checkpoint | `col-` and 16 hex characters of a hash of your source's ID |
| Document | One file the connector wrote, with its metadata file and its record | `doc-` and 16 hex characters of a hash of your source's ID |

A destination holds many custom connectors, and each custom connector runs many jobs over time. The documents those jobs write fall into collections, and documents that name no collection go to the default collection, `col-default`.

## Destinations

A destination's type decides what reads its bucket.

| Type | What the framework creates | After a successful job |
| --- | --- | --- |
| `BEDROCK` | A bucket, an [Amazon Bedrock](https://aws.amazon.com/bedrock/) managed knowledge base, and a data source over the bucket | Starts the knowledge base's ingestion |
| `QUICK` | A bucket with metadata files in the [AWS Quick](https://aws.amazon.com/quick/) format | Nothing. You connect AWS Quick to the bucket, and AWS Quick syncs it. |
| `S3` | A bucket with plain metadata files | Nothing. Your own pipeline reads the bucket. |

A new destination starts in `CREATING_BUCKET`, moves through `CREATING_KNOWLEDGE_BASE` and `CREATING_DATA_SOURCE` for `BEDROCK`, and ends `ACTIVE` or `FAILED`. A `FAILED` destination's `status_reason` has the form `<error>: <cause> (correlation <id>)`. Deleting a destination moves it to `DELETING`.

## Custom connectors

A custom connector runs one job at a time, so its `status` is `IN_USE` while a job runs, and otherwise `AVAILABLE` or `DELETING`.

It holds `connector_configuration`, a string every job reads, and the ARN of a secret, whose value the toolkit reads through your [source-access role](tenant-roles.md#source-access-role). The framework encrypts the configuration and never parses either one, so your implementation decides what both mean.

## Jobs

A job starts on the custom connector's schedule, or when you call `StartCustomConnectorJob`. It moves from `STARTED` to `RUNNING`, and ends `SUCCEEDED` or `FAILED`. `StopCustomConnectorJob` moves it through `STOPPING` to `STOPPED`. A failed job's `status_reason` says why, and names the log group and stream when the custom connector has a log destination.

A job can carry a `job_configuration` string, for example to read one part of the source again. A scheduled job has none.

## Collections and checkpoints

A collection groups documents that come and go together, such as the files of one repository or the pages of one wiki space. Deleting a collection deletes its documents.

Each collection can hold a checkpoint, a string your implementation keeps between jobs, such as the time it last read that part of the source. The next job uses it to read only what changed. After a failed job, the next one resumes from the checkpoints the failed job saved. [Store documents and checkpoints](document-store.md) shows how.

## Documents

Each document is a file and its metadata file in the destination bucket, under the custom connector's own prefix:

```text
connector_id=<connector ID>/collection_id=<collection ID>/document_id=<document ID>/<file name>
```

The framework also keeps a record of each document: its source ID, the hash of its content, and its source version. The records let the next job find the documents that changed and delete the ones the source no longer has. The document's content stays in your bucket and never passes through the framework's API.
