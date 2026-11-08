---
description: Build and run a connector with the Custom Connector Framework (CCF), what a connector developer needs from the administrator, and the order of the tasks in this guide.
---

# Developer Guide

The Custom Connector Framework (CCF) is an open source AWS sample that simplifies building, running, and operating document ingestion from any document source into [Amazon Bedrock knowledge bases](https://aws.amazon.com/bedrock/knowledge-bases/), [AWS Quick](https://aws.amazon.com/quick/), or [Amazon S3](https://aws.amazon.com/s3/). You write the code that reads your source. CCF manages everything else.

As a connector developer, you use a CCF deployment that your administrator hosts to keep a knowledge base current with a document source that has no built-in connector. CCF starts a job on your connector's schedule, and its Python toolkit writes what your code reads into your own AWS account.

Everything your connector touches stays in your account, called the tenant account: the destination bucket, the knowledge base, the connector image, the source's secret, and the connector's logs. The framework acts there only through roles your stacks create. [Roles in your account](tenant-roles.md) shows each one.

You don't host the framework. The [Administrator Guide](../administrator-guide/index.md) covers hosting it.

## What you need from your administrator

Ask your administrator to add your account's ID to the framework's allowed tenant accounts, and to give you:

- The framework values, for the `framework` setting of your configuration files: its API endpoint, its service account's ID, its Batch execution role's ARN, and its destination workflow's ARN. [Onboard tenant accounts](../administrator-guide/tenant-accounts.md) describes them.
- The Region the framework runs in. Your destination and connector stacks deploy in that Region.

When the framework runs in your own account, as in the [tutorial](../getting-started/tutorial.md), you need none of these. Your stacks import the framework's exports.

## Your tasks

You build and run a connector in this order:

1. Learn the [concepts](concepts.md): destinations, custom connectors, jobs, collections, and documents.
1. [Create a destination](destinations.md): the bucket and, for Amazon Bedrock, the knowledge base your documents go to.
1. [Write a connector implementation](connector-implementation.md) in Python, and [store documents and checkpoints](document-store.md) through the toolkit.
1. [Deploy a connector](deploy-a-connector.md): build its image and register a custom connector for each source.
1. [Search your documents](search.md) with Amazon Bedrock, AWS Quick, or your own agents.
1. [Troubleshoot a job](troubleshooting.md) when one fails.

The example [GitHub connector](../github-connector/index.md) does all of this for GitHub repositories, and is the template a new connector copies.

## Prerequisites

Before you deploy anything, you need:

- Credentials for your tenant account in your shell, and the account and Region bootstrapped for the [AWS CDK](https://aws.amazon.com/cdk/).
- [uv](https://docs.astral.sh/uv/getting-started/installation/), Node.js 22 or 24, and the [AWS CLI](https://aws.amazon.com/cli/).
- A running container builder for synthesis. CDK uses Docker. To use [Finch](https://runfinch.com/), run `export CDK_DOCKER=finch`. The connector image itself is built by [AWS CodeBuild](https://aws.amazon.com/codebuild/) during the deploy.
- A clone of the [repository](https://github.com/aws-samples/sample-custom-connector-framework-for-knowledge-bases), with `make install` run at its root.
