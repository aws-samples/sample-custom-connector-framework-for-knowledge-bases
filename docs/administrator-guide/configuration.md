---
description: Every setting of the Custom Connector Framework's configuration file, its default, and what it controls.
---

# Configure the service

The framework's [AWS CDK](https://aws.amazon.com/cdk/) app, in `framework/cdk`, reads one JSON configuration file. It starts from `config/default.json`, which holds every setting's default, and lays your file over it:

- Your file lists only the settings it changes. A setting it leaves out keeps its default.
- Inside `apiThrottling`, `jobCompute`, and `alarms`, each member merges on its own.
- A list, such as `allowedTenantAccountIds`, replaces the default.
- An unknown or invalid setting fails synthesis, and the error names your file and the setting.

The app has no built-in environments, such as dev and prod. To deploy a different setup, write another configuration file.

## Example

`config/example-disposable.json` is a test setup whose resources are deleted with their stacks:

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

## Settings

Each setting, its default, and what it controls:

| Setting | Default | What it controls |
| --- | --- | --- |
| `removalPolicy` | `RETAIN` | `RETAIN` keeps the service key, the tables, the queues, and the log groups when a stack is deleted. `DESTROY` deletes them. |
| `allowedTenantAccountIds` | `[]` | The accounts, besides the service account, that may call the API. The framework may assume their destination roles (`role/ccf/destinations/CcfDestinationRole-*`), and the [AWS Batch](https://aws.amazon.com/batch/) execution role may pull from their `ccf-connectors/*` repositories. The service account is always allowed and must not be listed. |
| `apiThrottling.rateLimit` | `100` | The steady-state requests per second the API stage admits across all callers. |
| `apiThrottling.burstLimit` | `200` | The requests the API stage admits at once above the steady rate. |
| `deletionProtectionThreshold` | `15` | The largest percentage, 0 to 100, of a `BEDROCK` destination's indexed documents that one ingestion may delete. The framework sets it on every [Amazon Bedrock](https://aws.amazon.com/bedrock/) data source it creates. |
| `logRetentionDays` | `30` | The retention of every framework log group, one of the [CloudWatch Logs retention periods](https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/Working-with-log-groups-and-streams.html#SettingLogRetention). |
| `costCenter` | `"unassigned"` | The value of the `cost-center` tag on both stacks. |
| `jobCompute.maxVcpus` | `256` | The [AWS Fargate](https://aws.amazon.com/fargate/) vCPU limit across all running jobs, 1 to 10,000. |
| `jobCompute.natGateways` | `1` | The NAT gateways in the job VPC. `1` costs less. `2`, one per Availability Zone, keeps jobs reaching the internet when a zone fails. |
| `alarms.apiLatencyMs` | `5000` | The API's p99 latency, 1 to 29,000 ms, above which its alarm fires. |
| `alarms.apiServerErrorPercent` | `5` | The API's percentage of 5xx responses, 1 to 100, above which its alarm fires. |

Settings an administrator has no reason to change, such as [AWS Lambda](https://aws.amazon.com/lambda/) memory, the ingestion queue's retries, and the dead-letter queues' retention, are constants in `SERVICE_SETTINGS` in `framework/cdk/lib/common/config.ts`.
