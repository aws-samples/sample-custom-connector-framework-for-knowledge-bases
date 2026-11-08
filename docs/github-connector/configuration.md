---
description: Every setting of the example GitHub connector's configuration file, and how to deploy it.
---

# Configuration

The GitHub connector's [AWS CDK](https://aws.amazon.com/cdk/) app, in `connectors/github/cdk`, reads one JSON configuration file, `config/default.json` by default, and needs every setting except `framework`. An unknown or mistyped setting fails synthesis, and the error names it. `config/example-small.json` reads five repositories of one organization. Its `framework` values are placeholders that show the cross-account form, so it doesn't deploy as it is.

## Deploy

The connector writes to an existing destination, so deploy the destination first:

1. Deploy a destination, as [Create a destination](../developer-guide/destinations.md) describes, and set its `DestinationId` output as `destination.id` in your configuration file.
1. From the repository root, run:

    ```bash
    make -C connectors/github deploy CONFIG=config/default.json
    ```

    `CONFIG` is relative to `connectors/github/cdk`.

1. Store a GitHub token that can read public repositories in the secret named by the `GitHubTokenSecretArn` output of `GitHubCustomConnector`, as the [operations runbook](operations.md#missing-expired-or-rejected-token) shows.

## Settings

| Setting | In `default.json` | What it controls |
| --- | --- | --- |
| `sources` | `ORG` entries for `aws-samples`, `awslabs`, and `aws` | What the app reads, as `{"type", "name"}` entries. An `ORG` entry names an owner, an organization or a user, and reads its repositories. A `REPOSITORY` entry names one repository as `owner/name`. The app creates one custom connector per owner, named `github-<owner>` in lowercase. Synthesis warns about a `REPOSITORY` entry whose owner also has an `ORG` entry. |
| `owners` | `aws-samples` on Sunday 02:00 UTC, `awslabs` on Sunday 10:00, `aws` on Sunday 18:00 | One entry per owner the sources name, with `scheduleWeekDay` (`SUN` to `SAT`), `scheduleHourUtc` (0 to 23), and an optional `connectorCompute` that replaces the shared one for that owner. |
| `connectorCompute.cpu` | `2` | vCPUs for each job, at least 0.5. The log router takes 0.25 of them. |
| `connectorCompute.memoryMib` | `8192` | Memory for each job in MiB, at least 1,024. The log router takes 512 of it. |
| `connectorCompute.timeoutHours` | `12` | [AWS Batch](https://aws.amazon.com/batch/) stops a job that runs longer. |
| `connectorConfiguration` | See the rows below | The configuration string each custom connector gets, with its `owner` and `repositories` added. Synthesis checks it against the connector's schema, so a mistyped setting fails. |
| `connectorConfiguration.max_repositories` | `300` | The most repositories one job reads for an `ORG` owner, most recently pushed first. Names that match `excluded_repository_patterns` don't count. |
| `connectorConfiguration.max_repository_age_days` | `730` | The listing stops at the first repository without a push in this many days. A repository that ages out has its documents deleted. |
| `connectorConfiguration.excluded_repository_patterns` | `["aws-sdk*", "*-sdk", "*-sdk-*", "sdk-*", "aws-cdk-go", "api-models-aws", "aws-tools-for-powershell"]` | Repository names that match a pattern are left out. The defaults leave out the AWS SDKs and the repositories generated from the same service models: the Go bindings for the AWS CDK, the API models, and the PowerShell tools, which are documented elsewhere. `aws-cdk-go` alone is about 23 GB. |
| `connectorConfiguration.excluded_path_patterns` | 53 patterns for build output, dependencies, binaries, and secrets | Files that match a pattern, in `.gitignore` syntax, are skipped and counted as `excluded_path` in the `files_skipped` log event. |
| `connectorConfiguration.max_file_bytes` | `104857600` (100 MiB) | Larger files are skipped and counted as `too_large`. |
| `connectorConfiguration.full_read_interval_days` | `28` | Days between full reads, when the connector reads every repository again even if its last push hasn't changed. |
| `connectorConfiguration.github.request_interval_seconds` | `2` | Seconds between GitHub API requests. |
| `connectorConfiguration.github.request_timeout_seconds` | `30` | The timeout of one GitHub request. |
| `connectorConfiguration.github.retry_attempts` | `5` | Retries of a failed GitHub request or a broken archive download. |
| `connectorConfiguration.github.retry_backoff_seconds` | `2` | The backoff factor between retries. |
| `connectorConfiguration.github.max_rate_limit_wait_seconds` | `3900` | The longest the connector waits for GitHub's rate limit to reset. |
| `command` | `["github_connector:GitHubConnectorImplementation"]` | The implementation class the image's entry point runs. |
| `maxImageAgeDays` | `90` | Every image in the repository expires this many days after its push, including the one the custom connectors run. A deploy pushes a new image only when the source changed, so deploy a source change before the running image expires, or every job fails to pull it. |
| `logRetentionDays` | `30` | The retention of the connector log groups, the image build's log group, and the providers' log groups. |
| `logDelivery` | `true` | `true` gives each custom connector a log group and a log-delivery role and sets its log destination, so its output is kept. |
| `removalPolicy` | `RETAIN` | `RETAIN` keeps the token secret, the image repository, and the log groups when the stack is deleted. |
| `destination.id` | `dst-0123456789ab`, a placeholder | The `DestinationId` output of the destination stack the custom connectors write to. |
| `imageRepositoryName` | `ccf-connectors/github` | The connector image repository. It must start with `ccf-connectors/`. |
| `sourceAccessServices` | `[]` | Service prefixes, such as `execute-api`, the source-access roles may call besides [Amazon S3](https://aws.amazon.com/s3/), [AWS Secrets Manager](https://aws.amazon.com/secrets-manager/), [AWS KMS](https://aws.amazon.com/kms/), and [Amazon CloudWatch Logs](https://aws.amazon.com/cloudwatch/). `iam`, `sts`, and `organizations` are refused. |
| `costCenter`, `team` | `unassigned` | The values of the `cost-center` and `team` tags on the stack. |
| `framework` | Not set | The framework values, when the framework runs in another account. See [What you need from your administrator](../developer-guide/index.md#what-you-need-from-your-administrator). |

## Index your own organizations

To index your own organizations, replace the `sources` and `owners` entries with your owners, and give each owner a schedule. Spread the schedules out, so the jobs don't run at the same time against one token's rate limit. The connector reads public repositories.
