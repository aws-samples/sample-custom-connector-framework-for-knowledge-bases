# CDK infrastructure

The [AWS Cloud Development Kit (AWS CDK)](https://aws.amazon.com/cdk/)
application deploys the CCF control plane, state, scheduling, connector
compute, and destination resources. The framework stack contains [Amazon
DynamoDB](https://aws.amazon.com/dynamodb/) state, a customer-managed [AWS
Key Management Service (AWS KMS)](https://aws.amazon.com/kms/) key, the
[Amazon S3](https://aws.amazon.com/s3/) ingestion bucket, [Amazon API
Gateway](https://aws.amazon.com/api-gateway/), backend [AWS
Lambda](https://aws.amazon.com/lambda/) functions, one shared [Amazon
EventBridge Scheduler](https://aws.amazon.com/eventbridge/) target, and
framework-account [AWS Batch](https://aws.amazon.com/batch/) compute.
`provider=bedrock` also creates an [Amazon Bedrock](https://aws.amazon.com/bedrock/)
Knowledge Base on Amazon S3 Vectors and its S3 data source.

## Destination mode

The `provider` context selects one destination for the complete framework
deployment:

```bash
npx cdk synth CcfDevStack --context env=dev --context provider=s3
npx cdk synth CcfDevStack --context env=dev --context provider=bedrock
npx cdk synth CcfDevStack --context env=dev --context provider=quick
```

- `s3` creates the ingestion bucket without a managed knowledge base.
- `bedrock` creates a Bedrock Knowledge Base, S3 Vectors storage, and a data
  source for the ingestion bucket.
- `quick` creates the ingestion bucket and Amazon Quick sidecar contract. Create
  the Amazon Quick knowledge base against that bucket separately; Amazon Quick
  Index reads it on its own schedule.

The toolkit's Bedrock adapter writes each document's access control list to its
metadata sidecar. The current CDK L1 data-source construct does not expose the
Bedrock document-level access-control setting. Turn that setting on through the
Bedrock connector API or console before relying on those ACLs.

## Framework deployment contract

The framework deploy uses these contexts:

| Context                   | Requirement                        | Purpose                                                                            |
| ------------------------- | ---------------------------------- | ---------------------------------------------------------------------------------- |
| `env`                     | Optional; defaults to `dev`        | Chooses `CcfDevStack` or `CcfProdStack` and state retention behavior               |
| `provider`                | Optional; defaults to `s3`         | Selects `s3`, `bedrock`, or `quick` for the deployment                             |
| `allowedClientAccountIds` | Required for cross-account clients | Comma-separated 12-digit client account IDs admitted by the API and framework role |
| `embeddingModelId`        | Optional for `bedrock`             | Overrides `amazon.titan-embed-text-v2:0`                                           |
| `vectorDimension`         | Optional for `bedrock`             | Overrides `1024`; it must match the embedding model                                |
| `costCenter`              | Required only for `env=prod`       | Supplies the production cost-allocation tag                                        |

A framework deployment exposes these client coordinates as stack outputs:

- `ServiceEndpoint`
- `ServiceRestApiId`
- `ServiceExecutionPrincipalArn`

The client also needs the framework account ID and Region used for the deployment.
Together these values form `CcfServiceReference`. The REST API ID remains a
separate coordinate because the client stack builds exact `execute-api` ARNs
without parsing an endpoint token.

The framework execution role is shared across every connector and is the
identity client stacks grant ECR pull access to; it is not the role the
running connector container holds. See [Starting and running a
job](../README.md#starting-and-running-a-job) in the root README for the
full execution-role-versus-job-role distinction and what the per-connector
runtime role is scoped to.

## Generic client stacks

`lib/sample-app.ts` selects one definition from a table and instantiates the same
`CustomConnectorFrameworkClientStack` for `SampleWebCrawlerStack`,
`SampleGitHubStack`, or `SampleGitLabStack`. The stack creates a source role, a
`linux/amd64` connector image asset, and one API registration custom resource.
It does not read framework tables, queues, Batch resources, destination buckets,
KMS keys, or fixed framework SSM parameters, and it creates no client-owned
schedule Lambda. A supplied schedule is part of the typed registration request;
CCF creates and invokes it in the framework account.

The same stack deploys your own connector. Build an image from your connector
definition and the toolkit, then instantiate `CustomConnectorFrameworkClientStack`
with your `connectorImage` (the build directory and Dockerfile), a `command` of
`ccf-connector run your_module:definition`, your `connectorConfiguration`, the
Secrets Manager ARNs the source role may read in `sourceSecretArns`, and an
optional `schedule`, `cpu`, `memoryMib`, and `timeoutSeconds`. `synthSampleStack`
in `lib/sample-app.ts` is a complete example: it builds the `serviceReference`
from the framework's outputs, publishes the image to the
`ClientImageAssetsFoundationStack` repository, and applies cdk-nag.

Every sample synth needs these contexts:

- `sample=web-crawler|github|gitlab`
- `apiEndpoint`
- `frameworkRestApiId`
- `frameworkAccount`
- `frameworkExecutionPrincipalArn`
- `frameworkRegion`
- `sourceAccessExternalId`

GitHub also needs `tokenSecretArn` and `repositoryFullName`, with optional
`githubApiUrl`. GitLab needs `tokenSecretArn` and `projectId`, with optional
`gitlabServerUrl`. The crawler has no source-secret context. The client and
framework Regions must match.

The client stack outputs `ConnectorImageUri` and `ConnectorId`. It passes typed
global configuration directly to the registration custom resource, so the
configuration is readable by anyone who can view the client stack's
CloudFormation template. Keep secrets out of it: the backend rejects credential
values, so GitHub and GitLab configuration contains an exact [AWS Secrets
Manager](https://aws.amazon.com/secrets-manager/) ARN reference, never a
token, and only the source role can read that secret.

The retained `ClientImageAssetsFoundationStack` owns the dedicated [Amazon
Elastic Container Registry (Amazon ECR)](https://aws.amazon.com/ecr/) repository
and its exact three-action pull policy for the framework principal. CDK must
publish sample image assets to an existing repository, so deploy the foundation
before the first sample stack and use the same contexts for both commands. The
sample stack dependency preserves ordering on later deployments.

The web crawler needs no source secret, so it is the fastest way to see a
sample deployed. Deploy the framework first and capture its outputs:

```bash
npx cdk deploy CcfDevStack --context env=dev --context provider=s3 \
  --outputs-file cdk.out/framework-outputs.json
```

Then deploy the foundation and the sample, reading the framework's outputs
and generating a fresh external ID:

```bash
npx cdk deploy ClientImageAssetsFoundationStack SampleWebCrawlerStack \
  --context sample=web-crawler \
  --context apiEndpoint="$(jq -r '.CcfDevStack.ServiceEndpoint' cdk.out/framework-outputs.json)" \
  --context frameworkRestApiId="$(jq -r '.CcfDevStack.ServiceRestApiId' cdk.out/framework-outputs.json)" \
  --context frameworkAccount="$CDK_DEFAULT_ACCOUNT" \
  --context frameworkExecutionPrincipalArn="$(jq -r '.CcfDevStack.ServiceExecutionPrincipalArn' cdk.out/framework-outputs.json)" \
  --context frameworkRegion="$CDK_DEFAULT_REGION" \
  --context sourceAccessExternalId="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" \
  --outputs-file cdk.out/web-crawler-outputs.json
```

The deploy registers the connector with a daily schedule. The schedule has no
start date, so [EventBridge Scheduler starts the first run as soon as the
schedule is created](https://docs.aws.amazon.com/scheduler/latest/UserGuide/schedule-types.html#rate-based)
and then repeats every 24 hours. To start another run yourself, call
`CcfClient` from the repository root with a profile that may invoke the API's
start route. A start request returns a conflict error while the connector
already has a run in progress:

```bash
CCF_ENDPOINT="$(jq -r '.CcfDevStack.ServiceEndpoint' cdk/cdk.out/framework-outputs.json)" \
CONNECTOR_ID="$(jq -r '.SampleWebCrawlerStack.ConnectorId' cdk/cdk.out/web-crawler-outputs.json)" \
uv run --directory toolkit python -c '
import os
from ccf_api_models.models import StartCustomConnectorJobRequest
from custom_connector_framework import CcfClient
from pydantic import SecretStr
client = CcfClient(endpoint=os.environ["CCF_ENDPOINT"], region=os.environ["CDK_DEFAULT_REGION"])
job = client.start_custom_connector_job(StartCustomConnectorJobRequest(
    connector_id=os.environ["CONNECTOR_ID"], job_configuration=SecretStr("{}"))).job
print(job.job_id, job.status.value)
'
```

See [the non-production Bedrock smoke procedure](../docs/non-production-bedrock-smoke-test.md)
for the same flow with job polling and Bedrock ingestion verification.

## Layout

The framework stack in `lib/stacks/` is a composition root for the focused
construct groups in `lib/construct-groups/`: encryption, storage, database,
Batch compute, runtime identities, delivery, API service, and the optional
knowledge base. Shared names and destination selection live in
`lib/common/config.ts`; the client-facing reference type lives in
`lib/common/ccf-service-reference.ts`.

## Development

`make install` runs `npm ci` and installs the locked Python test environment.
`make check` type-checks, lints, runs CDK assertions, runs cdk-nag, and runs the
focused Python registration tests. A synth or deploy bundles backend and
registration Lambda assets and builds the connector image, so it requires a
running container builder. Set `CDK_DOCKER=finch` to build with Finch instead
of Docker.
