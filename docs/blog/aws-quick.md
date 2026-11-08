---
title: Bring any document source into AWS Quick with a custom connector
description: Index a document source that AWS Quick has no built-in connector for, such as your GitHub organizations, with the Custom Connector Framework, through an Amazon Bedrock knowledge base or the Amazon S3 integration in AWS Quick.
---

# Bring any document source into AWS Quick with a custom connector

[AWS Quick](https://aws.amazon.com/quick/) answers questions from the knowledge bases you connect, and it ships connectors for many common sources. Your teams also keep knowledge in systems no connector covers: an internal wiki, a ticketing tool, or the hundreds of repositories in your GitHub organizations. Until that content is in a knowledge base, AWS Quick can't answer from it.

In this post, we show how to bring such a source into AWS Quick with the [Custom Connector Framework](../index.md). We use the example GitHub connector, which indexes the public repositories of the organizations you choose, and we cover both ways the framework reaches AWS Quick.

![Banner titled "Bring more of your knowledge into AWS Quick", with the line "Connect a document source once. CCF keeps it current for AWS Quick." GitHub organizations, internal wikis, and ticketing systems connect to the Custom Connector Framework hub. The hub connects to AWS Quick through Amazon Bedrock or Amazon S3. Below the hub are faster time to value, an index that stays current, you decide what AWS Quick sees, and data stays with its owner.](../assets/diagrams/blog-aws-quick.png)

## What the Custom Connector Framework is

The Custom Connector Framework (CCF) is an open source AWS sample that simplifies building, running, and operating document ingestion from any document source into [Amazon Bedrock knowledge bases](https://aws.amazon.com/bedrock/knowledge-bases/), AWS Quick, or [Amazon S3](https://aws.amazon.com/s3/). You write the code that reads your source. CCF manages everything else.

For teams that use AWS Quick, that brings these outcomes:

**Faster time to value.** CCF provides **managed connector infrastructure**, so your team writes only the code that reads the source, and AWS Quick answers from it sooner.

**An index that stays current.** CCF manages full and incremental syncs. Each scheduled job writes only the documents that changed and removes the ones your connector reports are gone, so nobody runs a manual import.

**Your data stays in your account.** Documents, the knowledge base, and the source's credentials **stay in the team's own AWS account**.

**You decide what AWS Quick sees.** Your connector chooses **which documents to include**, which attributes to attach, and who may read each one.

## Why index your GitHub organizations

A large engineering organization writes the same code more than once. One team builds a retry wrapper, a deployment pipeline, or an ingestion job, and another team builds it again because they never found the first one. Repository search finds names, not intent, so "an [AWS Step Functions](https://aws.amazon.com/step-functions/) workflow that calls Amazon Bedrock" is hard to find unless you know which repository to open.

An index changes that. When AWS Quick can search every repository by meaning, anyone can ask whether something already exists before they build it, and get the files that do it, with links. The same index serves the AI agents your teams use to write code.

Keeping that index current is the hard part. A GitHub organization can hold thousands of repositories, pushed to every day, and some are deleted or renamed. A connector has to read them on a schedule, rewrite the files that changed, and delete the ones that are gone. It also has to wait out the GitHub API rate limits, and pick up where it stopped when a job fails. CCF manages the schedule, the record of what each job wrote, and the checkpoints a job resumes from. The connector's own code reads GitHub and paces its requests.

## Solution overview

The framework separates the administrator, who hosts it in a service account, from the connector developer, who owns the documents in a tenant account. A job runs your connector, which writes new and changed documents to an Amazon S3 bucket in the tenant account. That bucket, with whatever indexes it, is called a destination. [How it works](../getting-started/how-it-works.md) shows the flow end to end.

AWS Quick reaches a destination in either of these ways:

- **Through an Amazon Bedrock knowledge base.** A `BEDROCK` destination comes with an Amazon Bedrock managed knowledge base, which the framework updates after each successful job. You connect that knowledge base to AWS Quick as a knowledge source. The same knowledge base also serves `Retrieve` calls from your applications and agents.
- **Through the Amazon S3 integration in AWS Quick.** A `QUICK` destination is a bucket whose metadata files are in the format AWS Quick reads. AWS Quick builds its own knowledge base from the bucket.

| | `BEDROCK` destination | `QUICK` destination |
| --- | --- | --- |
| Who indexes the documents | Amazon Bedrock, started by the framework after each job | AWS Quick, from the bucket |
| Who else can search | Your applications and agents, with `Retrieve` | AWS Quick only |
| Limits to plan for | Up to 2 managed knowledge bases per AWS Quick instance, in the same AWS Region | Requires an AWS Quick Enterprise subscription. Cross-account access only within one Region. |
| Document-level access | ACL-enabled knowledge bases, with AWS Quick passing each user's identity | Access control lists that you turn on when you connect the bucket |

Choose `BEDROCK` when agents or applications search too, and `QUICK` when only AWS Quick does.

## Prerequisites

To follow along, you need:

- The framework and the GitHub connector deployed, as the [tutorial](../getting-started/tutorial.md) describes. The tutorial creates a `BEDROCK` destination.
- An AWS Quick instance in the same AWS Region, and an AWS Quick administrator who can add AWS resources and knowledge bases.
- For the `QUICK` path, an AWS Quick Enterprise subscription.

## Connect a BEDROCK destination to AWS Quick

The tutorial's destination stack, `CustomConnectorDestinationForGitHub`, outputs `KnowledgeBaseId`. These steps connect that knowledge base to AWS Quick, as [Bring your own Amazon Bedrock managed knowledge base](https://docs.aws.amazon.com/quick/latest/userguide/quick-byo-bedrock-kb.html) describes.

1. Build the knowledge base's ARN, `arn:aws:bedrock:<region>:<account>:knowledge-base/<KnowledgeBaseId>`.
1. If AWS Quick runs in another AWS account, attach a resource policy to the knowledge base that grants the AWS Quick service role `bedrock:Retrieve` and `bedrock:GetDocumentContent`. Same-account setups don't need one.
1. In AWS Quick, an administrator adds the ARN under **Admin** > **AWS Resources**.
1. Under **Integrations**, create a knowledge base, and choose **Bedrock knowledge base** as the data source type.
1. Add the knowledge base to a space, and ask a question in chat, such as "Is there a sample that runs a Step Functions workflow on a schedule?"

AWS Quick answers from the passages it retrieves, with links to the files on GitHub. Each scheduled job, weekly by default, keeps the knowledge base current, so new repositories and changes show up without anyone running an import.

## Use a QUICK destination instead

A `QUICK` destination skips the Amazon Bedrock knowledge base. The GitHub app's stack name and its custom connector names are fixed, so one account runs one copy of it. To switch, delete `GitHubCustomConnector` first, then:

1. Copy `connectors/github/cdk/config/destination.json`, change `name` to `GitHubQuick`, `type` to `QUICK`, and remove `metadataFields`, which only a `BEDROCK` destination accepts. Deploy it as [Create a destination](../developer-guide/destinations.md) describes, and note its `BucketName` output.
1. Set the destination's `DestinationId` as `destination.id` in your GitHub configuration, and deploy the connector again, as [Configuration](../github-connector/configuration.md) describes.
1. In AWS Quick, an administrator grants access to the bucket, as [Grant Amazon Quick access to Amazon S3 buckets](https://docs.aws.amazon.com/quick/latest/userguide/s3-admin-setup.html#s3-grant-bucket-access) describes.
1. Create a knowledge base from the bucket with the [Amazon S3 integration](https://docs.aws.amazon.com/quick/latest/userguide/s3-integration.html), and add it to a space.

## Clean up

To stop indexing, delete the connector stack, then the destination stack, as the tutorial's [clean-up step](../getting-started/tutorial.md#step-6-clean-up) describes. Then remove the knowledge base from AWS Quick under **Integrations**.

## Conclusion

In this post, we showed how to bring a source that has no built-in connector into AWS Quick, through a `BEDROCK` or a `QUICK` destination. AWS Quick can answer from that source once a current copy of it is in a knowledge base. CCF keeps that copy current: it manages the schedule, syncs only what changed, and keeps your documents in your account. With your GitHub organizations indexed, your teams can find the code that already exists before they write it again.

To index a source of your own, read [Build a custom connector for any document source](build-a-connector.md). To give your coding agents the same index, read [Let Strands Agents find the code your teams already wrote](strands-agents.md).
