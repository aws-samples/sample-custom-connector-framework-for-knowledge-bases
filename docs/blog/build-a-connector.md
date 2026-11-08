---
title: Build a custom connector for any document source
description: Build a custom connector for Amazon Bedrock knowledge bases and AWS Quick with the Custom Connector Framework. Write only the code that reads your document source, and let the framework manage the schedule, full and incremental syncs, and the security boundary.
---

# Build a custom connector for any document source

[Amazon Bedrock knowledge bases](https://aws.amazon.com/bedrock/knowledge-bases/) and [AWS Quick](https://aws.amazon.com/quick/) ship connectors for common sources, and both can index an [Amazon S3](https://aws.amazon.com/s3/) bucket. For any other document source, you need code that keeps a current copy of its documents in a bucket. Writing the code that reads the source is the small part. Running it reliably is the large part.

In this post, we show how to build a connector with the Custom Connector Framework, so the only code you write is the part that knows your source.

![Banner titled "Faster time to production for every connector", with the line "You write the code that reads your source. CCF manages everything else." Your code, which reads the source, connects to a hub labeled "Your connector on CCF". Around the hub are what CCF manages: schedules, the security boundary, ingestion, retries and resume, and full and incremental sync. Below the hub are faster time to production, managed connector infrastructure, you control what's ingested, and a simpler toolkit than raw SDKs.](../assets/diagrams/blog-build-a-connector.png)

## What the Custom Connector Framework is

The Custom Connector Framework (CCF) is an open source AWS sample that simplifies building, running, and operating document ingestion from any document source into Amazon Bedrock knowledge bases, AWS Quick, or Amazon S3. You write the code that reads your source. CCF manages everything else: the API, the schedule, the compute, full and incremental sync, retries, ingestion, and the alarms. It also manages the security boundary between the account that runs your code and the account that owns your data.

Dividing the work this way is what changes the effort. Built from scratch, the infrastructure around a connector takes weeks, and every team that needs a new source builds it again. With CCF, your team starts on **managed connector infrastructure** that is already running. It writes only its source's reader, and gets **faster time to production** and a searchable knowledge base sooner.

CCF also manages **full and incremental syncs**. It records every document a job wrote, writes only the documents that changed, and removes the ones your code reports are gone. Your documents and the source's credentials **stay in your own AWS account**.

You stay in control of the source. Your code decides **which documents are ingested**, which attributes they carry, and who may read them. You choose when each custom connector runs, and your code paces its requests, so a third-party API is used within its limits.

Your code calls a small toolkit to put a document, delete one, or save a checkpoint, instead of signing API requests and managing Amazon S3 keys. An administrator hosts CCF once, and every team uses the same API, toolkit, and constructs.

## What a connector has to do

A connector that keeps a knowledge base current has to do more than read the source:

- Run on a schedule, sometimes for hours, without a server to manage.
- Write only the documents that changed, so a weekly sync doesn't rewrite and re-index everything.
- Delete the documents the source no longer has, so search doesn't return stale answers.
- Pick up where it stopped when a job fails, instead of starting over.
- Start the knowledge base's ingestion when it's done.
- Keep the source's credentials and the documents in the account that owns them.

CCF manages this work once, for every connector. It runs each job on a schedule, for as long as the source takes to read. Its toolkit skips documents whose content hasn't changed, tells your code which documents are gone from the source, and saves your checkpoints. After a successful job, CCF starts the knowledge base's ingestion. It reaches your account only through roles you own, which [Security](../administrator-guide/security.md) describes.

## Solution overview

You write a connector implementation: a Python class with one method, `run()`, that reads the source and hands documents to the toolkit's document store. You package it as a container image, and an [AWS Cloud Development Kit (AWS CDK)](https://aws.amazon.com/cdk/) construct registers it with the framework as a custom connector. Each scheduled run starts one job, and each job calls `run()` once.

The example GitHub connector is a complete implementation. It treats each repository as a collection of documents, and saves the repository's last push time as the collection's checkpoint. It skips a repository whose last push hasn't changed, so a weekly job over an organization reads only the repositories that changed that week. Every 28 days by default, it reads every repository again.

## Prerequisites

To follow along, you need:

- The framework deployed, and a destination to write to. The [tutorial](../getting-started/tutorial.md) deploys both in one account.
- [uv](https://docs.astral.sh/uv/getting-started/installation/), Node.js 22 or 24, and a clone of the [repository](https://github.com/aws-samples/sample-custom-connector-framework-for-knowledge-bases).

## Step 1: Write the implementation

Your class subclasses `ConnectorImplementation`. `self.job` holds the job's configuration and the source's secret, and `self.store` takes the documents. This implementation reads a wiki, one space at a time:

```python
from custom_connector_framework import ConnectorImplementation, DeleteDocumentRequest, DiffRequest, SourceDocument


class WikiConnector(ConnectorImplementation):
    def run(self) -> None:
        wiki = connect_to_wiki(self.job)  # your source client
        for space in wiki.spaces():
            pages = wiki.pages(space.key)
            for page in pages:
                self.store.put(page.to_put_request(collection_id=space.key))
            current = tuple(SourceDocument(document_id=page.id) for page in pages)
            for page_id in self.store.diff(DiffRequest(current=current, collection_id=space.key)).missing:
                self.store.delete(DeleteDocumentRequest(document_id=page_id, collection_id=space.key))
```

In this example, `connect_to_wiki`, `spaces`, `pages`, and `to_put_request` stand for your source's own client. The store writes a document on `put` only when its content changed. Its `diff` method returns the documents the store holds that the source no longer has, so the job deletes them.

## Step 2: Read only what changed

A large source takes too long to read in full every time. Save a checkpoint per collection, such as the time you last read a space, and read only the pages changed since. Saving a collection's checkpoint first sends that collection's writes. When a job fails partway, the spaces it finished keep their documents and checkpoints. The next job resumes from there. [Store documents and checkpoints](../developer-guide/document-store.md#example-read-only-what-changed) extends the example with checkpoints and a periodic full read.

## Step 3: Test it locally

The toolkit's `testing` module runs your implementation against in-memory fakes of the framework API and Amazon S3, so you can test it without an AWS account. A test seeds stored documents, runs the job, and checks what it wrote and deleted. [Test it locally](../developer-guide/connector-implementation.md#test-it-locally) shows a test.

## Step 4: Deploy it

Package the class in a container image whose entry point is `ccf-connector run`. Then define a CDK stack with a `CustomConnectorProject` and one custom connector for each part of the source you read. The project builds your image and stores it in your account. [Deploy a connector](../developer-guide/deploy-a-connector.md) shows the stack, and the GitHub connector's stack is a working template.

After the deploy, if your connector reads a secret, store the source's credentials in it. Then start a job, or wait for its schedule.

## Clean up

Stop any running job first, because the framework doesn't delete a custom connector while its job runs. Then delete your connector's stack. For each custom connector, the framework deletes the schedule, the documents, and the records. For a `BEDROCK` destination, it then asks for an ingestion, so the knowledge base drops the documents.

## Conclusion

In this post, we showed how to build a connector on CCF, from the implementation to a local test and a deploy. Your code knows your source. CCF manages the schedule, writes only what changed, deletes what your code reports is gone, resumes from checkpoints, and starts ingestion. Your documents and credentials stay in your own account. The [Developer Guide](../developer-guide/index.md) covers each part in depth.

To put the result in front of your teams, read [Bring any document source into AWS Quick with a custom connector](aws-quick.md).
