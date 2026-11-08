---
description: Every term the Custom Connector Framework documentation uses, with a definition and an example.
---

# Glossary

Examples use `111122223333` as the tenant account and `444455556666` as the service account.

## Framework terms

These terms are the framework's own.

| Term | Definition | Example |
| --- | --- | --- |
| Service account | The AWS account where the framework is deployed. The administrator owns it. | `444455556666` |
| Framework values | The `CustomConnectorFramework` stack outputs a tenant deploys with: `ApiEndpoint`, `ServiceAccountId`, `BatchExecutionRoleArn`, and `DestinationWorkflowArn`. A stack in the service account imports them, and a tenant in another account copies them into its `framework` setting. | `"framework": {"endpoint": "https://abcdef1234.execute-api.us-east-1.amazonaws.com/ccf", ...}` |
| Tenant account | An AWS account that owns destinations, registers custom connectors, and holds the source-access roles and each destination's role. A connector developer owns it. | `111122223333` |
| Source | The document source a connector implementation reads, such as a code host or a wiki. | GitHub |
| Connector implementation | The code for one kind of source: a `ConnectorImplementation` subclass, packaged as a container image in the tenant's connector image repository. | The GitHub connector image `111122223333.dkr.ecr.us-east-1.amazonaws.com/ccf-connectors/github:<asset hash>` |
| Connector project | A CDK app that deploys one connector implementation: a `CustomConnectorProject`, which builds the image into its repository, the existing destination it writes to, and one custom connector per source. | The `GitHubCustomConnector` stack in `111122223333` |
| Connector image repository | An [Amazon ECR](https://aws.amazon.com/ecr/) repository in the tenant account, named `ccf-connectors/<name>`, that holds a connector project's images. Its repository policy lets only the Batch execution role pull. | `ccf-connectors/github` in `111122223333` |
| Custom connector | A registered, configured instance of a connector implementation that reads a source into a destination and holds its jobs. | `cc-e020d65d76c0`, named `github-awslabs`, runs the GitHub image with `{"owner": "awslabs", ...}`, a token secret, a weekly schedule, and its source-access role, and writes to `dst-7c41e9a2b5d0` |
| Destination | A tenant-owned resource in the tenant account: an [Amazon S3](https://aws.amazon.com/s3/) bucket and, for type `BEDROCK`, an [Amazon Bedrock](https://aws.amazon.com/bedrock/) knowledge base and data source that read only that bucket. Types `S3` and `QUICK` are a bucket only. | `arn:aws:ccf:us-east-1:111122223333:destination/dst-7c41e9a2b5d0`, type `BEDROCK` |
| Destination stack | The framework app's stack for one destination, `CustomConnectorDestinationFor<Name>`: the destination's role, its boundaries, and the destination. | `CustomConnectorDestinationForGitHub` in `111122223333` |
| Job | One run of a custom connector. | `ccj-5b8e0d2c41f9` |
| Collection | An optional group of documents in one custom connector, with an optional checkpoint. Its ID is `col-` plus 16 hex characters of the SHA-256 of the implementation's source collection ID. Documents that name no collection go to `col-default`. | `col-3f9a1c2b5d7e9f01`, the files of GitHub repository 630587223 |
| Collection checkpoint | A string the connector implementation keeps for one collection, often JSON, up to 16,384 bytes. The framework stores it encrypted and doesn't parse it. The toolkit returns it as the implementation's checkpoint model. `col-default`'s checkpoint is the custom connector's own checkpoint. | `{"pushed_at": "2026-10-30T18:22:04Z", "filters": "a41f"}` |
| Document | One file the connector implementation hands over, stored with a metadata file in the destination bucket. Its ID is `doc-` plus 16 hex characters of the SHA-256 of the implementation's source document ID. | `doc-8d41e07a9c3f2b6e`, from source document ID `630587223/README.md` |
| Document ARN | The identifier of a document everywhere. | `arn:aws:ccf:us-east-1:111122223333:destination/dst-7c41e9a2b5d0/custom-connector/cc-e020d65d76c0/collection/col-3f9a1c2b5d7e9f01/document/doc-8d41e07a9c3f2b6e` |
| Document prefix | The S3 key prefix that holds one document's file and metadata file, labeled with the IDs from the document ARN. | `connector_id=cc-e020d65d76c0/collection_id=col-3f9a1c2b5d7e9f01/document_id=doc-8d41e07a9c3f2b6e/` |
| Source version | An optional string from the source that the toolkit stores with a document and returns, and never reads. | The git blob SHA `3b18e512dba79e4c8300dd08aeb37f8e728b8dad` |
| Content hash | The toolkit's SHA-256 of a document's bytes and metadata, used to skip rewriting an identical file. | `sha256:9f2c...` |
| Metadata file | The `<file>.metadata.json` file beside a document that holds its attributes for Amazon Bedrock, [AWS Quick](https://aws.amazon.com/quick/), or whatever reads an `S3` destination. | `README.md.metadata.json` |
| Access control list | An optional list on a document, of `USER` and `GROUP` entries with `ALLOW` or `DENY`, that the store writes into the metadata file. | `[{"principal_id": "dev@example.com", "principal_type": "USER", "access": "ALLOW"}]` |
| Log destination | A custom connector's optional [Amazon CloudWatch Logs](https://aws.amazon.com/cloudwatch/) log group in the tenant account, and the log-delivery role that writes to it. Without one, nothing the container prints is kept. | `/ccf/connectors/github-awslabs` |
| Toolkit | The `custom_connector_framework` Python package, and the CDK constructs that `framework/cdk/lib/index.ts` exports. | `from custom_connector_framework import ConnectorImplementation` |
| `CcfClient` | The toolkit's typed client for the framework API. | `create_ccf_client(CcfClientSettings(endpoint=..., region="us-east-1", session=boto3.Session()))` |

## Roles

The framework reaches each account through these roles, and each tenant role trusts the framework only with its external ID.

| Term | Definition | Example |
| --- | --- | --- |
| Destination role | The role in the tenant account, one per destination, that the framework's workflows, API, and ingestion consumer assume to manage that destination. | `arn:aws:iam::111122223333:role/ccf/destinations/CcfDestinationRole-GitHub` |
| Source-access role | The role in the tenant account that a custom connector's job assumes, with the external ID, to read its secret and write its documents. | `arn:aws:iam::111122223333:role/CcfSourceRole-github-awslabs` |
| Log-delivery role | The role in the tenant account that the job's log router assumes, with the external ID, to write the container's output to the log destination. | `arn:aws:iam::111122223333:role/CcfLogDeliveryRole-github-awslabs` |
| Runtime role | The role the framework creates in the service account for each custom connector. The job's containers run as it. Its name is a hash, so the name alone doesn't reveal the custom connector. | `arn:aws:iam::444455556666:role/ccf-runtime/CcfConnectorRuntime-3c9e1f0a2b7d4e6f8a1b2c3d4e5f6a7b` |
| Batch execution role | The framework role [AWS Batch](https://aws.amazon.com/batch/) uses before the containers start, to pull the connector image and write the log router's own diagnostics. | `arn:aws:iam::444455556666:role/ccf/CcfBatchExecutionRole` |
| External ID | A value in a tenant role's trust policy that stops anyone from tricking the framework into assuming the role for them, the confused deputy problem. The framework generates one per custom connector. A destination role's external ID is a state machine's ARN. | `ccf-ext-2f9d41c7a8b04e1f9c3d5a6b7e8f9012` |

## AWS terms

These are standard AWS terms, defined in AWS documentation.

| Term | Meaning here |
| --- | --- |
| [Knowledge base](https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base.html) | An Amazon Bedrock managed knowledge base, which indexes documents for retrieval-augmented generation (RAG): an application retrieves the passages that match a question and hands them to a model to answer from. |
| [Data source](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-managed-ds-s3.html) | The knowledge base's connection to the destination bucket. |
| [Ingestion job](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent_StartIngestionJob.html) | One sync of the bucket into the knowledge base's index. |
| [AWS Quick knowledge base](https://docs.aws.amazon.com/quick/latest/userguide/s3-integration.html) | An index AWS Quick builds from an Amazon S3 bucket or from a connected Amazon Bedrock knowledge base, which chat and agents in AWS Quick search. |
| [External ID](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_common-scenarios_third-party.html) | The IAM condition that the framework's tenant roles require. |
