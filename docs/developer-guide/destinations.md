---
description: Configure, deploy, and delete a Custom Connector Framework destination, and declare a knowledge base's metadata fields.
---

# Create a destination

A destination is the bucket your documents go to and, for type `BEDROCK`, the [Amazon Bedrock](https://aws.amazon.com/bedrock/) knowledge base and data source that index it. The framework API manages destinations, through `CreateDestination`, `UpdateDestination`, and `DeleteDestination`. Changing a destination never redeploys the framework.

Your connector project deploys each destination as its own [AWS CloudFormation](https://aws.amazon.com/cloudformation/) stack in your tenant account, `CustomConnectorDestinationFor<Name>`, built from the framework's `CustomConnectorDestination` construct. The stack creates the destination's role in your account, then calls `CreateDestination`, and the framework builds the destination through that role. When you change the destination's metadata fields and redeploy the stack, it calls `UpdateDestination`.

Because the destination has its own stack, you can delete or replace a connector without losing the index.

## Choose a type

| Choose | When |
| --- | --- |
| `BEDROCK` | You search with Amazon Bedrock `Retrieve`, from your own agents, or from [AWS Quick](https://aws.amazon.com/quick/). The framework creates a managed knowledge base and starts its ingestion after each successful job. |
| `QUICK` | You search in AWS Quick through its [Amazon S3](https://aws.amazon.com/s3/) integration, and want AWS Quick to index the bucket itself. |
| `S3` | Your own pipeline reads the documents. |

AWS Quick can use both a `BEDROCK` destination and a `QUICK` destination. [Search your documents](search.md) compares them.

## Configure the destination

Every setting is required except `framework` and `metadataFields`, and none has a default. The example GitHub connector's destination stack reads `connectors/github/cdk/config/destination.json`:

```json
{
  "name": "GitHub",
  "type": "BEDROCK",
  "accessControlEnabled": false,
  "metadataFields": [
    { "name": "owner", "type": "STRING", "searchability": "ENABLED" },
    { "name": "repository", "type": "STRING", "searchability": "ENABLED" },
    { "name": "repository_id", "type": "STRING", "searchability": "ENABLED" },
    { "name": "path", "type": "STRING", "searchability": "ENABLED" },
    { "name": "title", "type": "STRING", "searchability": "ENABLED" }
  ],
  "removalPolicy": "RETAIN",
  "logRetentionDays": 30,
  "costCenter": "unassigned"
}
```

| Setting | What it controls |
| --- | --- |
| `name` | The destination's name: 1 to 64 letters, digits, `.`, `_`, or `-`. The stack is `CustomConnectorDestinationFor` plus the name in PascalCase, and the role is `CcfDestinationRole-<name>`. |
| `type` | `BEDROCK`, `QUICK`, or `S3`. |
| `accessControlEnabled` | Turns on access control lists. Only a `BEDROCK` destination accepts `true`, and it can't change after creation. See [Access control lists](document-store.md#access-control-lists). |
| `metadataFields` | The knowledge base's custom metadata fields. Only a `BEDROCK` destination accepts any. See [Declare metadata fields](#declare-metadata-fields). |
| `removalPolicy` | `RETAIN` keeps the destination, its documents, its role, and its boundaries when the stack is deleted. `DESTROY` deletes them. |
| `logRetentionDays` | How many days the log groups of the stack's destination provider, the custom resource that creates the destination, keep their logs. |
| `costCenter` | The value of the `cost-center` tag on the stack. |
| `framework` | The framework values, when the framework runs in another account. See [What you need from your administrator](index.md#what-you-need-from-your-administrator). |

## Deploy the destination

Deploy the destination's stack with credentials for your tenant account:

1. From the repository root, run:

    ```bash
    make -C connectors/github deploy-destination
    ```

    The target reads `config/destination.json` from `connectors/github/cdk`. To use another file, set `DESTINATION_CONFIG`, relative to `connectors/github/cdk`.

1. Wait for the deployment. It waits up to an hour for the destination to become `ACTIVE`, which takes several minutes for `BEDROCK`. If the destination ends `FAILED`, the deployment fails with its `status_reason`.

The stack outputs `DestinationId`, `BucketName`, and `DestinationRoleArn`, and, for `BEDROCK`, `KnowledgeBaseId` and `DataSourceId`. Your connector's configuration names the `DestinationId`.

A second destination in the same account is a second configuration file with another `name`.

## Declare metadata fields

A `BEDROCK` destination's metadata fields declare the document attributes its knowledge base knows about. You can filter a search on any attribute a document has, whether or not it's declared.

- A destination takes up to 30 fields, each with a `name`, a `type` (`STRING` or `STRING_LIST`), and a `searchability` (`ENABLED` or `DISABLED`), which is the Amazon Bedrock setting for whether the field is searchable.
- A name is the attribute's name in the documents' metadata files, such as `owner` or `repository` for the example GitHub connector. It can't start with `_`, which Amazon Bedrock reserves.
- A new destination has none, because Amazon Bedrock takes the fields only after the knowledge base exists. The deployment sets them with `UpdateDestination` after the destination is `ACTIVE`.
- A change to `metadataFields` keeps the destination, and the destination stays `ACTIVE` while jobs run.
- A field's type can't change. Remove the field in one deployment and add it back with the new type in the next.

`UpdateDestination` updates the knowledge base, then stores the list. If Amazon Bedrock accepts an update and then fails to apply it, the knowledge base and the stored list can differ. Send the same list again, or redeploy the destination stack, to set them both.

## Delete a destination

Delete every connector stack that writes to the destination first, because `DeleteDestination` refuses a destination that still has custom connectors. Then delete the destination's stack:

```bash
aws cloudformation delete-stack --stack-name CustomConnectorDestinationForGitHub
aws cloudformation wait stack-delete-complete --stack-name CustomConnectorDestinationForGitHub
```

What happens next depends on the destination's `removalPolicy`:

- With `removalPolicy` `DESTROY`, the framework empties and deletes the bucket, and deletes the knowledge base, the data source, the ingestion log delivery, and the knowledge base role.
- With `RETAIN`, the destination, its documents, its role, and its boundaries stay. To delete the destination later, call `DeleteDestination` while the framework still runs, then delete the role and its boundaries.
