# Administrator guide

You deploy the Custom Connector Framework (CCF) once, in the framework
account, and each destination and connector project in a tenant account. This
guide gives each app's configuration, the order to deploy in, and the roles
each side owns, then how to monitor, troubleshoot, and tear down. The
[glossary](../DESIGN.md#glossary) defines destination, tenant account, runtime
role, and the other terms. The [README](../README.md#walkthrough-from-deploy-to-search)
walks through a first deploy in one account.

## Prerequisites

To deploy any of the apps, you need:

- Credentials for the target account in your shell, and the account and Region
  bootstrapped for the AWS CDK.
- [uv](https://docs.astral.sh/uv/getting-started/installation/), Node.js 22 or
  24, and `make install` run at the repository root.
- A running container builder, because synthesis builds the Lambda bundles in
  containers. CDK uses Docker; run `export CDK_DOCKER=finch` to use
  [Finch](https://runfinch.com/). The connector image itself is built by
  AWS CodeBuild during the deploy.

Each app reads one JSON configuration file, relative to the app's `cdk/`
folder. For the framework and the GitHub connector, `make deploy` passes
`CONFIG` as `--context config=$(CONFIG)`, and `CONFIG` defaults to
`config/default.json`. For a destination, `make -C framework/cdk deploy`
passes `DESTINATION_CONFIG` as `--context destinationConfig=$(DESTINATION_CONFIG)`.
An unknown or mistyped key fails synthesis and the error names it. There are
no environments: to deploy a different setup, write another configuration
file.

## Configure the framework

The framework app (`framework/cdk`) reads `config/default.json`, which holds
every key's default, and then lays your file over it. Your file lists only the
keys it changes: a key it leaves out keeps its default, and inside
`apiThrottling`, `jobCompute`, and `alarms` each member merges on its own. A
list, such as `allowedTenantAccountIds`, replaces the default. An unknown or
invalid key fails synthesis, naming your file and the key.
`config/example-disposable.json` is a setup whose resources are deleted with
their stacks:

```json
{
  "removalPolicy": "DESTROY",
  "allowedTenantAccountIds": ["111122223333"],
  "apiThrottling": { "rateLimit": 20, "burstLimit": 40 },
  "deletionProtectionThreshold": 100,
  "logRetentionDays": 7,
  "costCenter": "ccf-dev"
}
```

| Key | Default | Effect |
| --- | --- | --- |
| `removalPolicy` | `RETAIN` | `RETAIN` keeps the service key, the tables, the queues, and the log groups when a stack is deleted; `DESTROY` deletes them. |
| `allowedTenantAccountIds` | `[]` | Accounts besides the service account that may call the API, whose destination roles (`role/ccf/destinations/CcfDestinationRole-*`) the framework may assume, and whose `ccf-connectors/*` repositories the Batch execution role may pull from. The service account is always allowed and must not be listed. |
| `apiThrottling.rateLimit` | `100` | Steady-state requests per second the API stage admits across all callers. |
| `apiThrottling.burstLimit` | `200` | Requests the API stage admits at once above the steady rate. |
| `deletionProtectionThreshold` | `15` | The largest percentage, 0 to 100, of a `BEDROCK` destination's indexed documents one ingestion may delete. Set on every data source the framework creates. |
| `logRetentionDays` | `30` | Retention of every framework log group, one of CloudWatch Logs' retention periods. |
| `costCenter` | `"unassigned"` | Value of the `cost-center` tag on both stacks. |
| `jobCompute.maxVcpus` | `256` | The Fargate vCPU cap across all running connector jobs, 1 to 10,000. |
| `jobCompute.natGateways` | `1` | NAT gateways in the job VPC: `1` costs less, `2` (one per Availability Zone) keeps jobs reaching the internet when a zone fails. |
| `alarms.apiLatencyMs` | `5000` | The API's p99 latency, 1 to 29,000 ms, above which its alarm fires. |
| `alarms.apiServerErrorPercent` | `5` | The API's 5xx percentage, 1 to 100, above which its alarm fires. |

Settings a deployer has no reason to change, such as Lambda memory and the
dead-letter queues' retention, are constants in `SERVICE_SETTINGS` in
`framework/cdk/lib/common/config.ts`.

## Configure a destination

A destination's file, such as `framework/cdk/config/destination-github.json`,
sets every key but `framework` and `metadataFields`. There are no defaults to
merge.

| Key | In `destination-github.json` | Effect |
| --- | --- | --- |
| `name` | `GitHub` | The destination's name: 1 to 64 letters, digits, `.`, `_`, or `-`. The stack is `CustomConnectorDestinationFor` plus the name in PascalCase, and the role is `CcfDestinationRole-<name>`. |
| `type` | `BEDROCK` | `BEDROCK`, `QUICK`, or `S3`. |
| `accessControlEnabled` | `false` | Turns on access control lists. Only a `BEDROCK` destination accepts `true`. |
| `metadataFields` | `owner`, `repository`, `path`, and `title`, each `STRING` and `ENABLED` | The knowledge base's custom metadata fields, as `{"name", "type", "searchability"}` entries: up to 30, `type` `STRING` or `STRING_LIST`, `searchability` `ENABLED` or `DISABLED`. Only a `BEDROCK` destination accepts any. Not set means none. The deployment sets them with `UpdateDestination` once the destination is `ACTIVE`, and a change keeps the destination. A field's type can't change, so remove the field in one deployment and add it back in the next. |
| `removalPolicy` | `RETAIN` | `RETAIN` keeps the destination, its documents, its role, and its boundaries when the stack is deleted; `DESTROY` deletes them. |
| `logRetentionDays` | `30` | Retention of the destination provider's log groups. |
| `costCenter` | `unassigned` | Value of the `cost-center` tag on the stack. |
| `framework` | not set | The framework's values for a destination in another account. See [Deploy a tenant in another account](#deploy-a-tenant-in-another-account). |

## Configure the GitHub connector

The GitHub app (`connectors/github/cdk`) reads one file, `config/default.json`
by default, and needs every key but `framework`. `config/example-small.json`
reads five repositories of one organization. Its `framework` values are
placeholders: it shows the cross-account form and isn't meant to deploy as is.

| Key | In `default.json` | Effect |
| --- | --- | --- |
| `sources` | `ORG` entries for `aws-samples`, `awslabs`, and `aws` | What the app reads, as `{"type", "name"}` entries. An `ORG` entry names an owner, an organization or a user, and reads its repositories. A `REPOSITORY` entry names one repository as `owner/name`. The app creates one custom connector per owner, named `github-<owner>` in lowercase. Synthesis warns about a `REPOSITORY` entry whose owner also has an `ORG` entry. |
| `owners` | `aws-samples` on Sunday 02:00 UTC, `awslabs` on Sunday 10:00, `aws` on Sunday 18:00 | One entry per owner the sources name, with `scheduleWeekDay` (`SUN` to `SAT`), `scheduleHourUtc` (0 to 23), and an optional `connectorCompute` that replaces the shared one for that owner. |
| `connectorCompute.cpu` | `2` | vCPUs for each job, at least 0.5. The log router takes 0.25 of them. |
| `connectorCompute.memoryMib` | `8192` | Memory for each job in MiB, at least 1,024. The log router takes 512 of it. |
| `connectorCompute.timeoutHours` | `12` | Batch stops a job that runs longer. |
| `connectorConfiguration` | see the rows below | The configuration string each custom connector gets, with its `owner` and `repositories` added. Synthesis checks it against the connector's schema, so a mistyped key fails. |
| `connectorConfiguration.max_repositories` | `300` | The most repositories one job reads for an `ORG` owner, most recently pushed first. Names that match `excluded_repository_patterns` don't count. |
| `connectorConfiguration.max_repository_age_days` | `730` | The listing stops at the first repository without a push in this many days. A repository that ages out has its documents deleted. |
| `connectorConfiguration.excluded_repository_patterns` | `["aws-sdk*", "*-sdk", "*-sdk-*", "sdk-*", "aws-cdk-go", "api-models-aws", "aws-tools-for-powershell"]` | Repository names matching a pattern are left out. The defaults leave out the AWS SDKs and the repositories generated from the same service models (the CDK's Go bindings, the API models, and the PowerShell tools), which are documented elsewhere. `aws-cdk-go` alone is about 23 GB. |
| `connectorConfiguration.excluded_path_patterns` | 53 patterns for build output, dependencies, binaries, and secrets | Files matching a pattern, in `.gitignore` syntax, are skipped and counted as `excluded_path` in the `files_skipped` log event. |
| `connectorConfiguration.max_file_bytes` | `104857600` (100 MiB) | Larger files are skipped and counted as `too_large`. |
| `connectorConfiguration.full_read_interval_days` | `28` | Days between full reads, when the connector reads every repository again even if its last push hasn't changed. |
| `connectorConfiguration.github.request_interval_seconds` | `2` | Seconds between GitHub API requests. |
| `connectorConfiguration.github.request_timeout_seconds` | `30` | Timeout of one GitHub request. |
| `connectorConfiguration.github.retry_attempts` | `5` | Retries of a failed GitHub request or a broken archive download. |
| `connectorConfiguration.github.retry_backoff_seconds` | `2` | The backoff factor between retries. |
| `connectorConfiguration.github.max_rate_limit_wait_seconds` | `3900` | The longest the connector waits for GitHub's rate limit to reset. |
| `command` | `["github_connector:GitHubConnectorImplementation"]` | The implementation class the image's entry point runs. |
| `maxImageAgeDays` | `90` | Every image in the repository expires this many days after its push, including the one the custom connectors run. A deploy with changed source pushes a new image; redeploy before then. |
| `logRetentionDays` | `30` | Retention of the connector log groups, the image build's log group, and the providers' log groups. |
| `logDelivery` | `true` | `true` gives each custom connector a log group and log-delivery role and sets its log destination, so its output is kept. |
| `removalPolicy` | `RETAIN` | `RETAIN` keeps the token secret, the image repository, and the log groups when the stack is deleted. |
| `destination.id` | `dst-0123456789ab`, a placeholder | The `DestinationId` output of the destination stack the custom connectors write to. |
| `imageRepositoryName` | `ccf-connectors/github` | The connector image repository. Must start with `ccf-connectors/`. |
| `sourceAccessServices` | `[]` | Service prefixes, such as `dynamodb`, the source-access roles may call besides S3, Secrets Manager, AWS KMS, and CloudWatch Logs. `iam`, `sts`, and `organizations` are refused. |
| `costCenter`, `team` | `unassigned` | Values of the `cost-center` and `team` tags on the stack. |
| `framework` | not set | The framework's values for a connector in another account. See [Deploy a tenant in another account](#deploy-a-tenant-in-another-account). |

## Deploy the framework

The framework deploys once, in the service account, before any destination or
connector project.

1. Choose or write the framework's configuration file.
1. Run:

   ```bash
   make -C framework deploy CONFIG=config/default.json
   ```

   This deploys `CustomConnectorFramework`, then `CustomConnectorFrameworkOps`,
   which reads the first stack's exports.

1. Subscribe to the topic in the `AlarmTopicArnExport` output of
   `CustomConnectorFrameworkOps`. Every framework alarm notifies it on ALARM and
   on OK.

`CustomConnectorFramework` outputs the values a tenant needs: `ApiEndpoint`,
`ServiceAccountId`, `BatchExecutionRoleArn`, and `DestinationWorkflowArn`. It
also exports them, with names such as `CustomConnectorFramework-ApiEndpoint`,
for tenants in the same account.

## Deploy a destination

Each destination has its own stack, deployed after the framework. Run, from
the repository root:

```bash
make -C framework/cdk deploy DESTINATION_CONFIG=config/destination-github.json
```

The stack creates the destination's role, `CcfDestinationRole-<name>`, and its
boundaries, then registers the destination with `CreateDestination`. The
deployment waits up to an hour until the destination is `ACTIVE`, or fails
with the destination's `status_reason`. The stack outputs `DestinationId`,
`BucketName`, and `DestinationRoleArn`, and for `BEDROCK` also
`KnowledgeBaseId` and `DataSourceId`.

Every destination stack creates its own role, so a second destination in the
same account is a second configuration file with another `name`.

## Deploy a connector project

A connector stack, such as `GitHubCustomConnector`, deploys after its
destination is `ACTIVE`, because each custom connector it registers names the
destination's ID. Set that ID as `destination.id` in the GitHub configuration,
then run:

```bash
make -C connectors/github deploy CONFIG=config/default.json
```

The stack holds one `CustomConnectorProject`. Its `ConnectorImage` creates the
image repository, whose policy lets only the framework's Batch execution role
pull. CDK uploads the Docker build context, and an arm64 CodeBuild project
builds the image and pushes it with a tag of the source's hash. Each custom
connector registers only after the image exists. The framework deploys
first, because ECR checks that the repository policy's principal, the Batch
execution role, exists.

The GitHub stack then adds the token secret and one `CustomConnector` per
owner. Each has a source-access role and, when log delivery is on, a log
group and log-delivery role. Store the GitHub token in the secret named by the
`GitHubTokenSecretArn` output of `GitHubCustomConnector`.

Another connector project in that account needs a different
`imageRepositoryName`. Its custom connector names must also be new to the
account, because the role and log group names come from them.

## Deploy a tenant in another account

The walkthrough uses one account as both the service account and the tenant
account. A tenant in another account works the same way, with these changes.

1. In the framework's configuration, add the tenant account to
   `allowedTenantAccountIds`, and redeploy the framework.
1. Read these outputs of `CustomConnectorFramework` in the framework
   account:

   ```bash
   aws cloudformation describe-stacks --stack-name CustomConnectorFramework --query \
     "Stacks[0].Outputs[?contains(['ApiEndpoint','ServiceAccountId','BatchExecutionRoleArn','DestinationWorkflowArn'], OutputKey)]"
   ```

1. In the tenant account, copy them into the `framework` key of both the
   destination configuration and the GitHub configuration:

   ```json
   "framework": {
     "endpoint": "https://abcdef1234.execute-api.us-east-1.amazonaws.com/ccf",
     "serviceAccount": "444455556666",
     "batchExecutionRoleArn": "arn:aws:iam::444455556666:role/ccf/CcfBatchExecutionRole",
     "destinationWorkflowArn": "arn:aws:states:us-east-1:444455556666:stateMachine:CcfDestinationWorkflow"
   }
   ```

1. With credentials for the tenant account, deploy the destination, then the
   connector project, in the service Region. A destination stack in another
   Region fails synthesis.

CloudFormation exports can't be imported across accounts. With a `framework`
key, `FrameworkValues.fromConfig` takes the values as literals and imports no
framework export:

| Framework output | `framework` key |
| --- | --- |
| `ApiEndpoint` | `endpoint` |
| `ServiceAccountId` | `serviceAccount` |
| `BatchExecutionRoleArn` | `batchExecutionRoleArn` |
| `DestinationWorkflowArn` | `destinationWorkflowArn` |

`endpoint` must have the form
`https://<API ID>.execute-api.<region>.amazonaws.com/<stage>`. The framework's
alarms and metrics, such as `JobsCompleted`, aren't visible from the tenant
account.

## Roles

These roles take part in a job.

| Role | Account | Owner | Used by | For |
| --- | --- | --- | --- | --- |
| Batch execution role (`role/ccf/CcfBatchExecutionRole`) | framework | framework | AWS Batch and Fargate, before the containers start | Pull the connector image from the tenant's repository, write the log router's own diagnostics. Its credentials never reach the containers |
| Runtime role (one per custom connector) | framework | framework | The running containers (connector and log router), as their task role | Call the framework API (SigV4 plus the delegation check), assume the source-access role and, when set, the log-delivery role. No S3 or other permission |
| Source-access role | tenant | tenant | The runtime role, by `AssumeRole` with the external ID | Read `secret_arn`; write, read, list, and delete documents under the custom connector's prefix in the destination bucket; anything else in the tenant account the connector implementation needs |
| Log-delivery role (optional) | tenant | tenant | The log router, as the runtime role, by `AssumeRole` with the external ID | `logs:CreateLogStream` and `logs:PutLogEvents` on the log destination's log group |
| Destination role (`CcfDestinationRole-<name>`, one per destination) | tenant | tenant | The destination workflow, the connector deletion workflow, the API Lambda, and the ingestion consumer | Create and delete destination infrastructure, delete documents' files, start ingestion. Never used by the container |

The container runs as the runtime role and acts as the tenant only through the
source-access role session. The toolkit uses that one session for the secret,
the destination bucket, and the start-of-job sweep, and hands it to the
connector implementation as `JobContext.source_session`.

An external ID stops a third party from tricking a trusted service into
assuming a role on its behalf, the confused deputy problem. Every tenant role
that a framework principal assumes requires one. The framework generates one
per custom connector for both of its tenant roles. A state machine's ARN is
the external ID for a destination role.

### Destination role

`DestinationRole` creates `role/ccf/destinations/CcfDestinationRole-<name>`
for each destination, shortened with a hash when the name makes it longer
than 64 characters. Its trust policy, with `<p>` the partition, `<F>` the
framework account, and `<r>` the service Region:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Effect": "Allow", "Action": "sts:AssumeRole",
      "Principal": { "AWS": "arn:<p>:iam::<F>:role/ccf/CcfApiRole" },
      "Condition": { "StringEquals": {
        "sts:ExternalId": ["arn:<p>:states:<r>:<F>:stateMachine:CcfDestinationWorkflow"] } } },
    { "Effect": "Allow", "Action": "sts:AssumeRole",
      "Principal": { "AWS": "arn:<p>:iam::<F>:role/ccf/CcfIngestionConsumerRole" },
      "Condition": { "StringEquals": {
        "sts:ExternalId": ["arn:<p>:states:<r>:<F>:stateMachine:CcfDestinationWorkflow"] } } },
    { "Effect": "Allow", "Action": "sts:AssumeRole",
      "Principal": { "AWS": "arn:<p>:iam::<F>:role/ccf/CcfDestinationWorkflowRole" },
      "Condition": { "StringEquals": { "sts:ExternalId": [
        "arn:<p>:states:<r>:<F>:stateMachine:CcfDestinationWorkflow",
        "arn:<p>:states:<r>:<F>:stateMachine:CcfDestinationDeletionWorkflow"] } } },
    { "Effect": "Allow", "Action": "sts:AssumeRole",
      "Principal": { "AWS": "arn:<p>:iam::<F>:role/ccf/CcfConnectorDeletionWorkflowRole" },
      "Condition": { "StringEquals": {
        "sts:ExternalId": ["arn:<p>:states:<r>:<F>:stateMachine:CcfConnectorDeletionWorkflow"] } } }
  ]
}
```

When a task assumes a role, Step Functions sends the state machine's ARN as
the external ID and has no field for another value. So each state machine's ARN
is the external ID. The API Lambda and the ingestion consumer call STS
themselves and send the `CcfDestinationWorkflow` ARN.

Its inline policy, `CcfDestinationPermissions`, with `<T>` the tenant account
and `<name>` the destination's name:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "DestinationBuckets", "Effect": "Allow",
      "Action": ["s3:CreateBucket", "s3:DeleteBucket", "s3:PutBucketPolicy", "s3:PutEncryptionConfiguration",
                 "s3:PutBucketPublicAccessBlock", "s3:PutBucketTagging", "s3:ListBucket"],
      "Resource": "arn:<p>:s3:::ccf-dst-*" },
    { "Sid": "DestinationObjects", "Effect": "Allow",
      "Action": ["s3:DeleteObject", "s3:GetObject"], "Resource": "arn:<p>:s3:::ccf-dst-*/*" },
    { "Sid": "GlobalAccessControlList", "Effect": "Allow",
      "Action": "s3:PutObject", "Resource": "arn:<p>:s3:::ccf-dst-*/ccf-acl/*" },
    { "Sid": "CreateTaggedKnowledgeBase", "Effect": "Allow",
      "Action": "bedrock:CreateKnowledgeBase", "Resource": "*",
      "Condition": { "Null": { "aws:RequestTag/ccf:destination-id": "false" },
                     "ForAllValues:StringEquals": { "aws:TagKeys": ["ccf:destination-id"] } } },
    { "Sid": "TagTaggedKnowledgeBase", "Effect": "Allow",
      "Action": "bedrock:TagResource", "Resource": "arn:<p>:bedrock:<r>:<T>:knowledge-base/*",
      "Condition": { "Null": { "aws:RequestTag/ccf:destination-id": "false" },
                     "ForAllValues:StringEquals": { "aws:TagKeys": ["ccf:destination-id"] } } },
    { "Sid": "ManageTaggedKnowledgeBase", "Effect": "Allow",
      "Action": ["bedrock:CreateDataSource", "bedrock:UpdateKnowledgeBase", "bedrock:StartIngestionJob",
                 "bedrock:StopIngestionJob", "bedrock:AllowVendedLogDeliveryForResource"],
      "Resource": "arn:<p>:bedrock:<r>:<T>:knowledge-base/*",
      "Condition": { "Null": { "aws:ResourceTag/ccf:destination-id": "false" } } },
    { "Sid": "IngestionLogGroups", "Effect": "Allow",
      "Action": ["logs:CreateLogGroup", "logs:TagResource", "logs:PutRetentionPolicy",
                 "logs:PutResourcePolicy", "logs:DeleteLogGroup"],
      "Resource": ["arn:<p>:logs:<r>:<T>:log-group:/aws/vendedlogs/bedrock/knowledge-base/ccf-*",
                   "arn:<p>:logs:<r>:<T>:log-group:/aws/vendedlogs/bedrock/knowledge-base/ccf-*:*"] },
    { "Sid": "IngestionLogDeliveries", "Effect": "Allow",
      "Action": ["logs:PutDeliverySource", "logs:GetDeliverySource", "logs:DeleteDeliverySource",
                 "logs:PutDeliveryDestination", "logs:GetDeliveryDestination", "logs:DeleteDeliveryDestination",
                 "logs:CreateDelivery", "logs:GetDelivery", "logs:DeleteDelivery"],
      "Resource": ["arn:<p>:logs:<r>:<T>:delivery-source:ccf-*",
                   "arn:<p>:logs:<r>:<T>:delivery-destination:ccf-*",
                   "arn:<p>:logs:<r>:<T>:delivery:*"] },
    { "Sid": "DescribeIngestionLogDelivery", "Effect": "Allow",
      "Action": ["logs:DescribeDeliveries", "logs:DescribeResourcePolicies", "logs:DescribeLogGroups"],
      "Resource": "*" },
    { "Sid": "ReadAndDeleteKnowledgeBases", "Effect": "Allow",
      "Action": ["bedrock:GetKnowledgeBase", "bedrock:DeleteKnowledgeBase", "bedrock:GetDataSource",
                 "bedrock:DeleteDataSource", "bedrock:GetIngestionJob", "bedrock:ListIngestionJobs"],
      "Resource": "arn:<p>:bedrock:<r>:<T>:knowledge-base/*" },
    { "Sid": "CreateBoundedKnowledgeBaseRole", "Effect": "Allow",
      "Action": "iam:CreateRole", "Resource": "arn:<p>:iam::<T>:role/ccf/destinations/CcfKnowledgeBase-*",
      "Condition": { "StringEquals": {
        "iam:PermissionsBoundary": "arn:<p>:iam::<T>:policy/ccf/destinations/CcfDestinationBoundary-<name>" } } },
    { "Sid": "ManageKnowledgeBaseRole", "Effect": "Allow",
      "Action": ["iam:TagRole", "iam:DeleteRole", "iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:GetRole"],
      "Resource": "arn:<p>:iam::<T>:role/ccf/destinations/CcfKnowledgeBase-*" },
    { "Sid": "PassKnowledgeBaseRole", "Effect": "Allow",
      "Action": "iam:PassRole", "Resource": "arn:<p>:iam::<T>:role/ccf/destinations/CcfKnowledgeBase-*",
      "Condition": { "StringEquals": { "iam:PassedToService": "bedrock.amazonaws.com" } } }
  ]
}
```

`CreateKnowledgeBase` has no resource type, so its statement names `*` and
relies on the tag conditions. `UpdateKnowledgeBase` sets a knowledge base's
metadata fields, and passes its role again, which the `PassKnowledgeBaseRole`
statement allows. The read and delete statement has no tag
condition, because a knowledge base that is being deleted no longer resolves
its tags. The log delivery statements name the destination's log group,
delivery source, and delivery destination by their `ccf-` prefix, and any
delivery, because CloudWatch Logs gives each delivery a random ID. The
describe actions take no resource; `CreateDelivery` calls two of them to write
the log group's resource policy. The role's own permissions boundary,
`CcfDestinationRoleBoundary-<name>`, holds the same statements. The
`CcfDestinationBoundary-<name>` managed policy allows only `s3:GetObject*`,
`s3:GetBucket*`, and `s3:List*` on `ccf-dst-*` buckets and their objects, and
`cloudwatch:PutMetricData` in the `AWS/Bedrock/KnowledgeBases` namespace. So a
knowledge base role the workflow creates can only read destination buckets and
publish its own metrics.

### Source-access role

`CustomConnector` creates `CcfSourceRole-<connector name>` in the tenant
account. When the custom connector's name makes the role name longer than
IAM's 64 characters, the construct cuts the name to fit and adds a hash
suffix. Its trust policy, with `<F>` the service account, `<RT>` the custom
connector's runtime role ARN, and `<X>` its external ID, both returned by
`CreateCustomConnector` and `GetCustomConnector`:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "AWS": "arn:<p>:iam::<F>:root" },
    "Action": "sts:AssumeRole",
    "Condition": {
      "StringEquals": { "sts:ExternalId": "<X>" },
      "ArnEquals": { "aws:PrincipalArn": "<RT>" }
    }
  }]
}
```

The trust names the service account and pins the runtime role with
`aws:PrincipalArn`. It doesn't name the runtime role as its principal. IAM
rejects a role principal it can't resolve yet, and the API creates the
runtime role seconds before this one.

Its inline policy `CcfConnectorAccess`, with `<B>` the destination bucket and
`<C>` the custom connector ID:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "ConnectorPrefixObjects", "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
      "Resource": "arn:<p>:s3:::<B>/connector_id=<C>/*" },
    { "Sid": "ConnectorPrefixList", "Effect": "Allow",
      "Action": "s3:ListBucket", "Resource": "arn:<p>:s3:::<B>",
      "Condition": { "StringLike": { "s3:prefix": "connector_id=<C>/*" } } }
  ]
}
```

Grant the rest on the construct itself: the custom connector is the role's
grant principal, so `secret.grantRead(connector)` lets the job read its secret,
as the GitHub stack does. Both of the custom connector's roles have the
permissions boundary `CcfConnectorBoundary-<connector name>`. It allows only
S3, Secrets Manager, AWS KMS, CloudWatch Logs, and the services in
`sourceAccessServices`, so a grant to any other service has no effect.

### Log-delivery role

With log delivery on, `CustomConnector` also creates the log group
`/ccf/connectors/<connector name>` and `CcfLogDeliveryRole-<connector name>`,
with the same trust policy as `CcfSourceRole-<connector name>` and this inline
policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "WriteLogDestination", "Effect": "Allow",
      "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
      "Resource": "arn:<p>:logs:<r>:<T>:log-group:/ccf/connectors/<connector name>:*" }
  ]
}
```

The custom connector's `log_destination` names the log group and the role.
Without a log destination, nothing the container prints is kept. With one, the
job's log router assumes the log-delivery role and writes each line the
container prints to a stream named after the job ID. No connector output lands
in the service account either way.

### Controls on untrusted connector code

The framework treats every connector image as untrusted code. These controls
limit what a job can reach:

- The runtime role's boundary, `CcfConnectorRuntimeBoundary`, and its inline
  policy allow only `execute-api:Invoke` on the custom connector's
  own routes and `sts:AssumeRole` on its own source-access and log-delivery
  roles in its tenant account.
- Jobs run in private subnets behind one
  NAT gateway, in `CcfJobSecurityGroup`, which allows no inbound traffic.
- The job VPC's only VPC endpoint is an S3
  gateway endpoint with the policy below.
- The Batch execution role is
  only the job's execution role, so its credentials never reach a container.
- The job reaches the secret,
  the bucket, and the log group only through the source-access and log-delivery
  roles, which require the custom connector's external ID.

The S3 gateway endpoint's policy, with `<F>` the service account:

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

A job reaches S3 in the service Region through this endpoint, which
allows destination buckets and every bucket the service account doesn't own.
So a connector implementation can read, through `source_session`, source
buckets in any account other than the service account. In the framework's
Region, though, the only service-account buckets a job reaches are
destination buckets. When the tenant account is the service account, as in
the walkthrough, the tenant's own buckets count as service-account buckets.
Its source bucket must then be in another Region or another account.

Buckets in other Regions, in any account, are reached through the NAT gateway,
where the endpoint policy doesn't apply. Service-account buckets there are
protected by their own bucket policies and by the runtime role having no S3
permission. A job reads only what the tenant grants its source-access role.

## Monitor

`CustomConnectorFrameworkOps` holds the framework's alarms and the
`CcfOperations` dashboard, which charts the framework's own metrics. Each
alarm's description starts with its severity and names its section of the
[framework runbook](../framework/docs/runbooks/delivery-failures.md).

CloudFormation names each alarm after its construct, so the alarm IDs below
appear inside the alarm names, for example
`CustomConnectorFrameworkOps-AlarmsJobsFailedAlarm...`.

| Alarm | Severity | Fires when |
| --- | --- | --- |
| `SchedulerDeliveryFailures` | P1 | EventBridge Scheduler couldn't deliver a scheduled start |
| `SchedulerTargetDlqMessages`, `IngestionDlqMessages` | P1 | A message is in a dead-letter queue |
| `ApiLambdaErrors`, `ScheduledStartLambdaErrors`, `IngestionConsumerLambdaErrors` | P2 | A framework Lambda function failed |
| `ApiServerErrorRate` | P2 | More than 5% of API requests failed with a server error |
| `ApiLatency` | P2 | API p99 latency is above 5 seconds |
| `JobsFailed` | P2 | A job failed |
| `DestinationCreationFailed` | P2 | A destination failed to create |
| `DestinationWorkflowExecutionsFailed`, `ConnectorDeletionWorkflowExecutionsFailed`, `JobExecutionExecutionsFailed` | P2 | A workflow execution failed |

The GitHub connector adds no alarm, dashboard, or metric. Its
[runbook](../connectors/github/docs/runbooks/operations.md) triages GitHub
failures from the log events in each custom connector's log group.

Each `BEDROCK` destination also has these, in its own account:

- **Ingestion logs.** The destination workflow delivers the knowledge base's
  ingestion logs to `/aws/vendedlogs/bedrock/knowledge-base/ccf-<destination ID>`,
  kept for the framework's `logRetentionDays`. Bedrock records each ingestion
  job's progress and each document's crawl, sync, and index status there, with
  the error when a document fails. The
  [ingestion-failures](../framework/docs/runbooks/delivery-failures.md#ingestion-failures)
  section has a query for the failed documents.
- **Knowledge base metrics.** Bedrock publishes `Invocations`, `ClientErrors`,
  `ServerErrors`, and `Throttles` for `Retrieve`, and `RawDataSize` after each
  ingestion job, in the `AWS/Bedrock/KnowledgeBases` namespace, with the
  `KnowledgeBaseId` dimension.

## Troubleshoot a failed job

A failed job's cause is in its `status_reason`, which
`GetCustomConnectorJob` returns; `ListCustomConnectorJobs` with `status`
`FAILED` finds the failed jobs. The framework builds the reason from the
connector container's exit code and Batch's own status reason. It adds only
values it already holds, such as the source-access role's ARN and the secret's
ARN, so no text from the container reaches the service account.

| Exit code | `status_reason` | What to do |
| --- | --- | --- |
| 70 | `ConnectorImplementationError: <where>` | The implementation raised. Read the stack trace in the log stream. |
| 71 | `JobTerminated: <Batch reason>; <where>` | The job was stopped, by `StopCustomConnectorJob` or by reaching its timeout. The Batch reason says which. |
| 72 | `SourceAccessDenied: AccessDenied assuming <role ARN>` | The source-access role's trust policy doesn't name the runtime role and external ID. Compare it with [Source-access role](#source-access-role). |
| 73 | `SourceAccessFailed: AssumeRole failed for <role ARN>` | STS failed for another reason, for example a role that doesn't exist yet. |
| 74 | `SecretUnavailable: GetSecretValue failed for <secret ARN>` | The secret is missing, empty, or not readable by the source-access role. |
| 76 | `FrameworkApiUnavailable: the job couldn't read its inputs from, or send its records to, the framework API` | The job couldn't reach the API at its start, in its sweep, or in its final send. Check the API alarms. |
| other, or none | Batch's status reason | Out of memory, an image pull failure, or a log router that failed to start. |

`<where>` is `see log group <name>, stream <job ID>` when the custom connector
has a log destination, and `no log destination is set` otherwise. For codes 70
and 71, read that stream in the tenant account:

```bash
aws logs tail /ccf/connectors/<connector name> --log-stream-names <job ID>
```

Without a log destination, nothing the container printed was kept. Add one, or
turn on `logDelivery` in the GitHub configuration, and run the job again.

### Other problems

These problems don't come from a job:

| Problem | Cause and fix |
| --- | --- |
| A destination is `FAILED` | Its `status_reason` has the form `<error>: <cause> (correlation <id>)`. Search the `/aws/vendedlogs/states/CcfDestinationWorkflow` log group for the correlation ID, fix the cause, delete the destination, and create it again. The [runbook](../framework/docs/runbooks/delivery-failures.md#destination-creation-failures) has the steps. |
| A job stays `STARTED` or `RUNNING`, and its custom connector stays `IN_USE` | The job's `CcfJobExecution` execution ended without recording the job's end. The [user guide](./user-guide.md#jobs) describes how to end the job. |
| Image pulls fail after the framework was redeployed | When the Batch execution role is deleted and created again, IAM turns the principal in each tenant's repository policy into the old role's unique ID. Redeploy each connector stack to write the policy again. |
| A delete stays `DELETING` | A workflow execution failed. Read its error, fix the cause, and call the same delete operation again. |

## Tear down

Delete the stacks in the reverse order of the deploy, waiting for each one:
each connector stack, then each destination stack, then
`CustomConnectorFrameworkOps`, then `CustomConnectorFramework`.

- Deleting a connector stack deletes its custom connectors. The framework
  deletes each one's schedule, files, records, and runtime role.
- Deleting a destination stack whose `removalPolicy` is `DESTROY` deletes the
  destination: the framework empties and deletes the bucket and deletes the
  knowledge base, data source, and knowledge base role. With `RETAIN`, as in
  `config/destination-github.json`, the destination, its documents, its role,
  and its boundaries stay. If you no longer need the destination, call
  `DeleteDestination` before you delete the framework stacks, then delete the
  role and its boundaries by hand.
- With `removalPolicy` `RETAIN`, the framework keeps its service key, tables,
  queues, and log groups, and the GitHub app keeps the token secret, the image
  repository, and the connector log groups. Delete them by hand when you no
  longer need them, or deploy with `DESTROY`.

CloudFormation refuses to delete `CustomConnectorFramework` while a
same-account destination or connector stack imports its exports.

## Upgrade from an earlier version

A framework deployed from an earlier version of this repository can't be
updated in place. Its named Lambda functions have new logical IDs, so the
change set fails with `Resource ... already exists`. Replace the framework
stacks instead:

1. Delete `CustomConnectorFrameworkOps`, then `CustomConnectorFramework`,
   waiting for each.
1. Run `make -C framework deploy CONFIG=<your file>`, which deploys
   `CustomConnectorFramework` and then `CustomConnectorFrameworkOps`.

## Quotas and limits

[DESIGN.md](../DESIGN.md#quotas-and-limits) lists the batch sizes, value sizes,
name rules, and compute minimums.
