---
description: The concepts behind the Custom Connector Framework, and how a job keeps a knowledge base current, from its start to search.
---

# How it works

The Custom Connector Framework (CCF) is an open source AWS sample that simplifies building, running, and operating document ingestion from any document source into [Amazon Bedrock knowledge bases](https://aws.amazon.com/bedrock/knowledge-bases/), [AWS Quick](https://aws.amazon.com/quick/), or [Amazon S3](https://aws.amazon.com/s3/). You write the code that reads your source. CCF manages everything else.

## The concepts

A **knowledge base** indexes documents so that people and AI agents can search them by meaning. In [retrieval-augmented generation (RAG)](https://aws.amazon.com/what-is/retrieval-augmented-generation/), an application retrieves the passages in a knowledge base that match a question, and gives them to a model, which answers from them. Amazon Bedrock Knowledge Bases and AWS Quick both build knowledge bases.

A **source** is a system that holds the documents, such as a code host, a wiki, or a ticketing system. **Ingestion** copies a source's documents into the knowledge base and keeps the copy current as documents change and disappear.

A **connector** does the ingestion for one kind of source. Amazon Bedrock and AWS Quick ship connectors for common sources, and both can index an Amazon S3 bucket. So any document source can reach them, as long as a connector keeps a current copy of its documents in a bucket. A **custom connector** is one you write for a source that has no built-in connector, and CCF manages it.

## Who does what

The framework keeps the person who hosts it apart from the people who build connectors, each in their own AWS account:

- The **administrator** hosts the framework in the **service account**: its API, its workflows, and the compute that runs every job. The [Administrator Guide](../administrator-guide/index.md) is for them.
- The **connector developer** writes the code that reads a source, and owns the **tenant account** where the documents, the knowledge base, and the source's credentials live. The [Developer Guide](../developer-guide/index.md) is for them.

To try the framework, one person can be both, in one account, as the [tutorial](tutorial.md) does.

## The resources

A connector developer creates these resources through the framework's API:

- A **destination** is the bucket that documents go to and, for type `BEDROCK`, the Amazon Bedrock knowledge base that indexes it. The bucket and the knowledge base live in the tenant account. Type `QUICK` is a bucket that AWS Quick indexes, and type `S3` is a plain bucket.
- A **custom connector** registers the developer's code, a container image, against one destination, with its configuration, its secret, and its schedule.
- A **job** is one run of a custom connector.

## A job, end to end

Figure 1 shows the AWS services a job uses, and the order it uses them in.

![The CCF architecture. The CCF service account runs Amazon API Gateway, AWS Lambda, Amazon DynamoDB, EventBridge Scheduler, AWS Step Functions, AWS Batch, and the Amazon SQS ingestion queue. The tenant account holds AWS Quick, the Amazon Bedrock knowledge base, the Amazon S3 destination bucket, and the connector image in Amazon ECR. Numbered arrows follow a job from its start, through reading GitHub and writing files to the bucket, to search.](../assets/diagrams/architecture-overview.png)
_Figure 1: How a job keeps a knowledge base current. The administrator owns the service account, and the connector developer owns the tenant account._

1. A job starts. [Amazon EventBridge Scheduler](https://docs.aws.amazon.com/scheduler/latest/UserGuide/what-is-scheduler.html) starts it on the custom connector's schedule, or the developer calls `StartCustomConnectorJob` through [Amazon API Gateway](https://aws.amazon.com/api-gateway/). The framework's [AWS Lambda](https://aws.amazon.com/lambda/) function records the job in [Amazon DynamoDB](https://aws.amazon.com/dynamodb/).
1. The function starts the job's workflow in [AWS Step Functions](https://aws.amazon.com/step-functions/).
1. The workflow submits the job to [AWS Batch](https://aws.amazon.com/batch/), which runs it on [AWS Fargate](https://aws.amazon.com/fargate/).
1. AWS Batch pulls the developer's connector image from the tenant's [Amazon Elastic Container Registry (Amazon ECR)](https://aws.amazon.com/ecr/) repository.
1. The connector reads the source. In the example connector, that's the GitHub repositories of the organizations you choose.
1. The connector writes new and changed documents to the destination bucket in Amazon S3, and deletes the ones the source no longer has. It writes only through a role the tenant owns. It records each document with the framework API, which keeps no document content.
1. After a successful job on a `BEDROCK` destination, the workflow queues an ingestion request in [Amazon Simple Queue Service (Amazon SQS)](https://aws.amazon.com/sqs/).
1. The framework starts the knowledge base's ingestion through the destination's role. Amazon Bedrock indexes the bucket.
1. AWS Quick, an application, or an agent searches the knowledge base. For a `QUICK` destination, AWS Quick indexes the bucket itself.

The next job reads only what changed. A document whose content hasn't changed isn't written again, and the connector's checkpoints let it skip the parts of the source that haven't changed.

## Where to go next

Pick the page that matches what you want to do:

- To try it, follow the [tutorial](tutorial.md).
- To see how the framework keeps the accounts apart, read [Security](../administrator-guide/security.md).
- To build a connector for your own source, start with the [Developer Guide](../developer-guide/index.md).
- For each workflow step by step, see [Workflows](../architecture/workflows.md).
