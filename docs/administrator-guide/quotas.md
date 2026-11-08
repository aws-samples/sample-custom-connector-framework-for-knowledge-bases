---
description: The limits of the Custom Connector Framework's API, its resources, and its jobs.
---

# Quotas and limits

Most of these limits come from the API model and the framework's code. Only the API request rate and the running-jobs limit are configuration settings.

| Limit | Value |
| --- | --- |
| API requests | `apiThrottling.rateLimit` per second across all callers, 100 by default, with bursts up to `apiThrottling.burstLimit`, 200 by default |
| Documents per `BatchPutCustomConnectorDocuments` or `BatchDeleteCustomConnectorDocuments` | 1 to 99 |
| Collections per `BatchDeleteCustomConnectorCollections` | 1 to 100 |
| Items per list page (`max_results`) | 1 to 100 |
| `connector_configuration`, `job_configuration` | 65,536 UTF-8 bytes |
| Collection checkpoint | 2 to 16,384 UTF-8 bytes |
| Destination and custom connector names | 1 to 64 characters of `A-Z`, `a-z`, `0-9`, `.`, `_`, `-` |
| Description | 1 to 1,024 characters |
| Source document and collection IDs | 1 to 1,024 characters |
| Job compute | At least 0.5 vCPU and 1,024 MiB. The log router takes 0.25 vCPU and 512 MiB of it. |
| Running jobs | `jobCompute.maxVcpus` vCPUs in total, 256 by default |
| Destination creation through a destination stack | 1 hour before the deployment fails |
| `name` of a `CustomConnector` | Unique among the custom connectors in an account |
| Destinations per destination stack | 1 |
| Metadata fields per `BEDROCK` destination | 0 to 30, the knowledge base's limit. Names are 1 to 200 characters of `A-Z`, `a-z`, `0-9`, `_`, `-`, starting with a letter or digit. |
| Job records | Kept for 7 days after the job ends |

The AWS services the framework uses have their own quotas, such as the Amazon Bedrock [managed knowledge base quotas](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-managed-quotas.html) and [AWS Batch service quotas](https://docs.aws.amazon.com/batch/latest/userguide/service_limits.html).
