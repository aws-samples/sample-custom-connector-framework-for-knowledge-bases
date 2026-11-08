---
description: Allow another AWS account to use the Custom Connector Framework, and give its developers the values they deploy with.
---

# Onboard tenant accounts

A tenant account is an AWS account that owns destinations and registers custom connectors. The service account is always a tenant. Any other account must be in the configuration's `allowedTenantAccountIds` before it can call the API, and before the framework can assume its roles or pull its images.

A tenant must deploy in the service account's Region. A destination stack in another Region fails synthesis.

## Allow an account

To let an account use the service, add it to the configuration and redeploy.

1. Add the account's ID to `allowedTenantAccountIds` in your configuration file:

    ```json
    { "allowedTenantAccountIds": ["111122223333"] }
    ```

1. Deploy the service again, as [Deploy the service](deployment.md) describes.

## Give the developers the framework's values

[AWS CloudFormation](https://aws.amazon.com/cloudformation/) exports can't be imported across accounts, so a tenant in another account takes the framework's values as literals.

1. Read the framework values, which are outputs of `CustomConnectorFramework`:

    ```bash
    aws cloudformation describe-stacks --stack-name CustomConnectorFramework --query \
      "Stacks[0].Outputs[?contains(['ApiEndpoint','ServiceAccountId','BatchExecutionRoleArn','DestinationWorkflowArn'], OutputKey)]"
    ```

1. Send them to the tenant's developers. They copy them into the `framework` setting of their destination and connector configuration files:

    ```json
    "framework": {
      "endpoint": "https://abcdef1234.execute-api.us-east-1.amazonaws.com/ccf",
      "serviceAccount": "444455556666",
      "batchExecutionRoleArn": "arn:aws:iam::444455556666:role/ccf/CcfBatchExecutionRole",
      "destinationWorkflowArn": "arn:aws:states:us-east-1:444455556666:stateMachine:CcfDestinationWorkflow"
    }
    ```

Each framework output maps to one `framework` setting:

| Framework output | `framework` setting |
| --- | --- |
| `ApiEndpoint` | `endpoint` |
| `ServiceAccountId` | `serviceAccount` |
| `BatchExecutionRoleArn` | `batchExecutionRoleArn` |
| `DestinationWorkflowArn` | `destinationWorkflowArn` |

None of these values is a secret. The API admits a caller only by its [AWS Signature Version 4](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_sigv.html) signature and its account, and every tenant role trusts a framework role only with an external ID. See [Security](security.md).

## What the tenant sees

A tenant reads its own resources through the API, and reads its own logs, knowledge base metrics, and documents in its own account. The framework's alarms and metrics, such as `JobsCompleted`, stay in the service account. When a tenant's job fails, its `status_reason` names the cause, and the tenant troubleshoots it with [Troubleshoot a job](../developer-guide/troubleshooting.md).
