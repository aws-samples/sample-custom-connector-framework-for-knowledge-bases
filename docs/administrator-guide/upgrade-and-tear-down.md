---
description: Upgrade the Custom Connector Framework from an earlier version, and delete its stacks.
---

# Upgrade and tear down

Upgrading replaces the framework's stacks, and tearing down deletes them after the tenants delete theirs.

## Upgrade from an earlier version

A framework deployed from an earlier version of this repository can't be updated in place. Its named [AWS Lambda](https://aws.amazon.com/lambda/) functions have new logical IDs, so the change set fails with `Resource ... already exists`. Replace the framework stacks instead:

1. Delete `CustomConnectorFrameworkOps`, then `CustomConnectorFramework`, waiting for each.
1. Run `make -C framework deploy CONFIG=<your file>`, which deploys `CustomConnectorFramework` and then `CustomConnectorFrameworkOps`.

Replacing the stacks deletes and re-creates the [AWS Batch](https://aws.amazon.com/batch/) execution role, so image pulls fail until each tenant acts. When a role is deleted, [IAM](https://aws.amazon.com/iam/) replaces the role in each tenant's repository policy with the old role's unique ID, which the new role doesn't match. Ask each tenant to redeploy their connector stacks, which write the policy again.

## Tear down the service

Tenants delete their own stacks first, as [Deploy a connector](../developer-guide/deploy-a-connector.md#delete-a-connector) and [Create a destination](../developer-guide/destinations.md#delete-a-destination) describe. Otherwise, [AWS CloudFormation](https://aws.amazon.com/cloudformation/) refuses to delete `CustomConnectorFramework` while a destination or connector stack in the service account imports its exports.

1. Delete `CustomConnectorFrameworkOps`, and wait until it's deleted:

    ```bash
    aws cloudformation delete-stack --stack-name CustomConnectorFrameworkOps
    aws cloudformation wait stack-delete-complete --stack-name CustomConnectorFrameworkOps
    ```

1. Delete `CustomConnectorFramework` the same way.

With `removalPolicy` set to `RETAIN`, the framework keeps its service key, tables, queues, and log groups. Delete them by hand when you no longer need them, or deploy with `DESTROY` before you delete the stacks.
