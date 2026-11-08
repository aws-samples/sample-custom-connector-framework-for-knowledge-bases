---
description: What the administrator who hosts the Custom Connector Framework owns, and the order of the tasks in this guide.
---

# Administrator Guide

The Custom Connector Framework (CCF) is an open source AWS sample that simplifies building, running, and operating document ingestion from any document source into [Amazon Bedrock knowledge bases](https://aws.amazon.com/bedrock/knowledge-bases/), [AWS Quick](https://aws.amazon.com/quick/), or [Amazon S3](https://aws.amazon.com/s3/). Connector teams write the code that reads their source. CCF manages everything else.

As the administrator, you host CCF for those teams. You deploy it once, in one AWS account and Region called the service account, and you choose which other AWS accounts, called tenant accounts, may use it. One deployment serves every team, and you watch all of their jobs through one set of alarms, one dashboard, and one runbook. You run the service: you respond to its alarms, follow its runbook, and upgrade it.

You don't build connectors, and you don't own the documents they write. Each connector developer deploys their destination and their connector in their own tenant account, as the [Developer Guide](../developer-guide/index.md) describes. Their documents, their knowledge base, and their secrets stay in their account.

## What you own

Everything you own runs in the service account:

| You own | In the service account |
| --- | --- |
| The framework API | [Amazon API Gateway](https://aws.amazon.com/api-gateway/) and [AWS Lambda](https://aws.amazon.com/lambda/). Callers sign every request with AWS Signature Version 4. |
| The records of destinations, custom connectors, jobs, and documents | [Amazon DynamoDB](https://aws.amazon.com/dynamodb/) tables, encrypted with the framework's [AWS KMS](https://aws.amazon.com/kms/) key |
| The workflows that create and delete destinations and run jobs | [AWS Step Functions](https://aws.amazon.com/step-functions/) state machines |
| The compute that runs each job | [AWS Batch](https://aws.amazon.com/batch/) on [AWS Fargate](https://aws.amazon.com/fargate/), in a job VPC |
| Schedules and ingestion requests | [Amazon EventBridge Scheduler](https://docs.aws.amazon.com/scheduler/latest/UserGuide/what-is-scheduler.html) and [Amazon SQS](https://aws.amazon.com/sqs/) |
| Alarms, the dashboard, and alarm notifications | [Amazon CloudWatch](https://aws.amazon.com/cloudwatch/) and [Amazon SNS](https://aws.amazon.com/sns/) |

## Your tasks

You host the service in this order:

1. [Configure the service](configuration.md): choose the tenant accounts, the API's throttling, the job compute, the alarms, and the removal policy.
1. [Deploy the service](deployment.md) with the AWS CDK, and subscribe to its alarms.
1. [Onboard tenant accounts](tenant-accounts.md): allow an account, and give its developers the framework values they deploy with.
1. Review [Security](security.md): the account boundary, the data flow, the network a job runs in, and the roles on each side.
1. [Monitor the service](monitoring.md), and use the [runbook](runbook-delivery-failures.md) when an alarm fires.
1. [Upgrade and tear down](upgrade-and-tear-down.md) the service.

[Quotas and limits](quotas.md) lists the limits of the API and of jobs.

## Prerequisites

To deploy the service, you need:

- Credentials for the service account in your shell, and the account and Region bootstrapped for the [AWS CDK](https://aws.amazon.com/cdk/) (`npx cdk bootstrap aws://<account>/<region>`).
- [uv](https://docs.astral.sh/uv/getting-started/installation/), Node.js 22 or 24, and the [AWS CLI](https://aws.amazon.com/cli/).
- A running container builder, because synthesis builds the Lambda bundles in containers. CDK uses Docker. To use [Finch](https://runfinch.com/), run `export CDK_DOCKER=finch` in the shell you deploy from.
- A clone of the [repository](https://github.com/aws-samples/sample-custom-connector-framework-for-knowledge-bases), with `make install` run at its root.
