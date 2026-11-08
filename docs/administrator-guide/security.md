---
description: The Custom Connector Framework's security boundaries, what data crosses them, the network a job runs in, and the controls on connector code.
---

# Security

Security and compliance are a shared responsibility between AWS and you, as the [AWS shared responsibility model](https://aws.amazon.com/compliance/shared-responsibility-model/) describes. This page covers the part that this sample's design decides. It shows which account holds what, which data crosses between accounts and how, the network a job runs in, and how the framework limits connector code. Before you deploy the sample in an account that holds production data, review this page against your own security requirements.

## Account boundaries

The framework keeps the service and its tenants in separate AWS accounts.

- The **service account** runs the framework: the API, the tables, the workflows, the job compute, and the alarms. The [administrator](index.md) owns it.
- A **tenant account** owns everything a connector reads and writes: the destination bucket, the knowledge base, the connector image, the secret, and the log group. A [connector developer](../developer-guide/index.md) owns it. The service account is always a tenant too, for testing in one account.

No framework role trusts a tenant account, and the framework doesn't hold a credential for one. Every action in a tenant account runs as a role the tenant created there. That role trusts a named framework role only with an [external ID](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_common-scenarios_third-party.html). An external ID helps prevent the confused deputy problem, where a third party tricks the framework into acting on its behalf. Each tenant role's trust and permissions policies are in [Roles in your account](../developer-guide/tenant-roles.md).

Tenants reach the framework only through its API. The API requires [AWS Signature Version 4](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_sigv.html) on every request, and its resource policy admits only the service account and the accounts in `allowedTenantAccountIds`. Every call then checks that the resource belongs to the caller's tenant.

## Data flow

Figure 1 shows what a running job reaches, and over which path.

![In the CCF service account, a connector job runs as an AWS Batch job in the private subnets of the job VPC. It writes files to the tenant account's Amazon S3 destination bucket through an S3 gateway endpoint. Through a NAT gateway, over HTTPS, it reaches AWS STS, AWS Secrets Manager, and Amazon CloudWatch Logs in the tenant account. It also reaches the framework API and the source on the internet, such as GitHub. Numbers 1 to 6 mark the order of a job's steps.](../assets/diagrams/network-and-data-flow.png)
_Figure 1: The job's network paths, and the account each request lands in._

1. The job assumes the custom connector's source-access role with [AWS STS](https://docs.aws.amazon.com/STS/latest/APIReference/welcome.html), and, when log delivery is on, its log-delivery role. Both roles are in the tenant account.
1. With the source-access role's session, the job reads the custom connector's secret from [AWS Secrets Manager](https://aws.amazon.com/secrets-manager/).
1. The connector reads the source over the internet. In the example connector, that's GitHub.
1. With the source-access role's session, the job writes each document and its metadata file to the destination bucket in [Amazon S3](https://aws.amazon.com/s3/), through the job VPC's S3 gateway endpoint.
1. The job records each document's ID, content hash, and source version with the framework API. The document's content doesn't pass through the API.
1. With the log-delivery role's session, the job's log router writes everything the connector container prints to the custom connector's log group in [Amazon CloudWatch Logs](https://aws.amazon.com/cloudwatch/).

### What the service account stores

The service account keeps records about documents and jobs, and the configuration that runs them:

| Data | Where | How it's protected |
| --- | --- | --- |
| Destination, custom connector, job, collection, and document records: IDs, names, statuses, content hashes, source versions | [Amazon DynamoDB](https://aws.amazon.com/dynamodb/) tables | Encrypted with the framework's customer managed [AWS KMS](https://aws.amazon.com/kms/) key, with point-in-time recovery |
| A custom connector's configuration and a job's configuration | Amazon DynamoDB | Encrypted with the framework's AWS KMS key before it's stored, and marked sensitive in the API model, so the framework doesn't log it |
| Collection checkpoints, strings the connector keeps between jobs | Amazon DynamoDB | Encrypted with the framework's AWS KMS key, and marked sensitive |
| Ingestion and schedule messages | [Amazon SQS](https://aws.amazon.com/sqs/) queues | Encrypted with the framework's AWS KMS key, and the queues refuse requests without TLS |

The service account stores no document content, no secret value, and nothing the connector prints. A failed job's `status_reason` uses only values the framework already holds. They are the container's exit code, the status reason from [AWS Batch](https://aws.amazon.com/batch/), and the stored role, secret, and log destination ARNs.

### What the tenant account stores

The tenant account holds the documents, the index, the credentials, the output, and the image:

| Data | Where | How it's protected |
| --- | --- | --- |
| Documents and their metadata files | The destination bucket, `ccf-dst-<destination ID>-<account>-<region>` | SSE-S3 encryption, S3 Block Public Access, and a bucket policy that denies any request without TLS. Each custom connector's role writes only under its own prefix, `connector_id=<connector ID>/` |
| The index | The [Amazon Bedrock](https://aws.amazon.com/bedrock/) knowledge base, for a `BEDROCK` destination | The knowledge base's own role reads only its destination's bucket |
| The source's credentials | AWS Secrets Manager | Read only by the custom connector's source-access role |
| The connector's output | The custom connector's log group | Written only by its log-delivery role. Without a log destination, the output is discarded |
| The connector image | The tenant's [Amazon ECR](https://aws.amazon.com/ecr/) repository | The repository policy grants pull access only to the framework's Batch execution role |

The destination bucket uses SSE-S3, not a customer managed key, because the destination role has no AWS KMS permission. To cut off access to a destination's documents, delete the destination or change its bucket policy.

## Network requirements

Jobs run in a VPC that the framework creates in the service account, and they need only outbound access.

- Jobs run on [AWS Fargate](https://aws.amazon.com/fargate/) in private subnets, in the `CcfJobSecurityGroup` security group, which allows no inbound traffic. A network ACL on those subnets also admits only traffic from inside the VPC and replies to the jobs' own connections.
- Outbound HTTPS goes through a NAT gateway in a public subnet: to the source, to the framework API, and to the AWS APIs the job calls (AWS STS, Secrets Manager, CloudWatch Logs). `jobCompute.natGateways` sets one NAT gateway, or two for one per Availability Zone.
- The VPC has one VPC endpoint, an S3 gateway endpoint, and none for the framework's other services.
- The framework API is a public [Amazon API Gateway](https://aws.amazon.com/api-gateway/) endpoint that requires Signature Version 4. Tenants and jobs call it over HTTPS.

The S3 gateway endpoint's policy, with `<p>` the partition and `<F>` the service account:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "DestinationBuckets", "Effect": "Allow", "Principal": "*", "Action": "s3:*",
      "Resource": ["arn:<p>:s3:::ccf-dst-*", "arn:<p>:s3:::ccf-dst-*/*"] },
    { "Sid": "OtherAccountBuckets", "Effect": "Allow", "Principal": "*", "Action": "s3:*",
      "Resource": ["arn:<p>:s3:::*", "arn:<p>:s3:::*/*"],
      "Condition": { "StringNotEquals": { "aws:ResourceAccount": "<F>" } } }
  ]
}
```

The policy allows destination buckets and every bucket that another account owns. A connector can therefore read a source bucket in a tenant account through its source-access role, but not the service account's other buckets in the service Region. Requests to buckets in other Regions leave through the NAT gateway, where the endpoint policy doesn't apply. There, the service account's buckets rely on their own bucket policies, and on the job's role having no S3 permission.

When the tenant account is the service account, as in the [tutorial](../getting-started/tutorial.md), the tenant's own buckets count as service-account buckets. A source bucket must then be in another Region or another account.

## Controls on connector code

The framework treats every connector image as untrusted code, because the tenant writes it and the service account runs it. These controls limit what a job can reach:

- **The job's role.** Each custom connector has its own runtime role, the job's task role. Its inline policy and its permissions boundary, `CcfConnectorRuntimeBoundary`, allow only `execute-api:Invoke` on that custom connector's own API routes and `sts:AssumeRole` on its own source-access and log-delivery roles in its own tenant account. The role can't change its own tags, so it can't widen either.
- **No framework permissions.** The runtime role has no S3, DynamoDB, or other permission where the framework runs. The job reaches the secret, the bucket, and the log group only through the tenant's roles, which require the custom connector's external ID.
- **Image pulls.** The Batch execution role pulls the image and is only the job's execution role, so its credentials don't reach a container.
- **Output.** Every job runs a [Fluent Bit](https://fluentbit.io/) log router. It sends the connector's output to the tenant's log group, or discards it when the custom connector has no log destination, so connector output doesn't reach the framework's account.
- **Network.** Jobs accept no inbound traffic, and reach S3 only as the endpoint policy above allows.

## Roles in the service account

The framework's roles have fixed names under the path `/ccf/`, so a tenant's trust, bucket, and repository policies can name them. Runtime roles use the path `/ccf-runtime/`.

| Role | Used by | What it may do |
| --- | --- | --- |
| `CcfApiRole` | The API's Lambda function | Read and write the framework's tables, start workflows, create runtime roles under `/ccf-runtime/` with the runtime boundary, and assume destination roles with the destination workflow's ARN as the external ID |
| `CcfDestinationWorkflowRole` | The destination workflow | Assume destination roles to create and delete destinations |
| `CcfConnectorDeletionWorkflowRole` | The connector deletion workflow | Assume destination roles to delete a custom connector's documents |
| `CcfIngestionConsumerRole` | The ingestion consumer | Assume destination roles to start ingestion |
| `CcfBatchExecutionRole` | AWS Batch and AWS Fargate, before the containers start | Pull connector images from `ccf-connectors/*` repositories in allowed accounts, and write the log router's own diagnostics |
| A runtime role per custom connector | The job's containers | See [Controls on connector code](#controls-on-connector-code) |

No framework role holds an S3 or Amazon Bedrock permission on a tenant resource. The API's Lambda function, the workflows, and the ingestion consumer reach tenant resources only through the destination's role.

## Logging and audit

Each account keeps its own record of what the framework did there.

- The API logs each request's operation, status, and error type, and the AWS error code behind a failed AWS call. It doesn't log configuration or checkpoint values, which the API model marks sensitive.
- [AWS CloudTrail](https://aws.amazon.com/cloudtrail/) in each account records the AWS API calls each role makes, including every `AssumeRole` into a tenant account. The API and the ingestion consumer name each session after the destination or custom connector it acts for, such as `ccf-ingest-<destination ID>`, so each record shows what a call was for.
- VPC flow logs record the job VPC's traffic in a framework log group.
- Each `BEDROCK` destination delivers its knowledge base's ingestion logs to a log group the tenant owns. See [Troubleshoot a job](../developer-guide/troubleshooting.md).

## Before you use this sample with production data

This is a sample, not a managed AWS service. Before you use it with production data, consider:

- Turning on [Amazon GuardDuty](https://aws.amazon.com/guardduty/) and [AWS Security Hub](https://aws.amazon.com/security-hub/) in every account that runs or uses the framework.
- Adding S3 server access logging or S3 data events in CloudTrail for destination buckets. The destination role can't set up logging, so the tenant sets it up.
- Reviewing the connector code you allow to run, and the `sourceAccessServices` each connector project grants its source-access roles.
- Restricting which principals in each tenant account may call the API, with IAM policies on `execute-api:Invoke`.
