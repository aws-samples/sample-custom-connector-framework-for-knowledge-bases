---
description: Deploy the Custom Connector Framework's stacks in the service account, and subscribe to its alarms.
---

# Deploy the service

The service deploys as [AWS CloudFormation](https://aws.amazon.com/cloudformation/) stacks in the service account:

- `CustomConnectorFramework` holds the API, the tables, the workflows, and the job compute.
- `CustomConnectorFrameworkOps` holds the alarms and the dashboard. It reads the first stack's exports.

Deploy them before any tenant deploys a destination or a connector.

## Prerequisites

Before you deploy, you need:

- The [prerequisites](index.md#prerequisites) of this guide.
- A configuration file. See [Configure the service](configuration.md). `config/default.json` deploys with every default.

## Deploy the stacks

Run these steps from the repository root, with credentials for the service account.

1. Run:

    ```bash
    make -C framework deploy CONFIG=config/default.json
    ```

    `CONFIG` is relative to `framework/cdk`. The command deploys `CustomConnectorFramework`, then `CustomConnectorFrameworkOps`.

1. Find the ARN of the alarm topic in [Amazon SNS](https://aws.amazon.com/sns/). It's the `AlarmTopicArnExport` output of `CustomConnectorFrameworkOps`:

    ```bash
    aws cloudformation describe-stacks --stack-name CustomConnectorFrameworkOps \
      --query "Stacks[0].Outputs[?OutputKey=='AlarmTopicArnExport'].OutputValue" --output text
    ```

1. Subscribe to the topic, for example by email. Every framework alarm notifies it when it changes to ALARM and back to OK.

    ```bash
    aws sns subscribe --topic-arn "<AlarmTopicArnExport>" --protocol email --notification-endpoint "<your address>"
    ```

## Result

`CustomConnectorFramework` outputs the framework values, which a tenant deploys with. It also exports them, with names such as `CustomConnectorFramework-ApiEndpoint`, which a destination or connector stack in the service account imports.

| Output | What it is |
| --- | --- |
| `ApiEndpoint` | The framework API's URL, `https://<API ID>.execute-api.<region>.amazonaws.com/ccf` |
| `ServiceAccountId` | The service account's ID |
| `BatchExecutionRoleArn` | The role that pulls connector images from tenant repositories |
| `DestinationWorkflowArn` | The destination workflow's ARN, the external ID every destination role requires |

To let another account use the service, see [Onboard tenant accounts](tenant-accounts.md).
