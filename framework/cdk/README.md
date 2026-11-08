# Framework CDK app and constructs

This [AWS Cloud Development Kit (AWS CDK)](https://aws.amazon.com/cdk/) project holds an app and a library. The app deploys the framework's two stacks
in the service account, and one destination stack per destination. The
library, published to the npm workspace as `custom-connector-framework-cdk`,
holds the constructs that destination and connector projects use in the tenant
account.

| Stack                                 | Holds                                                                                                                                                                                                                                                                                                           |
| ------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `CustomConnectorFramework`            | The service key, the DynamoDB tables, the API, the Lambda functions, AWS Batch compute and its VPC, the runtime role boundary and Batch execution role, the scheduler group, the destination and connector deletion workflows, the `CcfJobExecution` state machine that runs every job, and the ingestion queue |
| `CustomConnectorFrameworkOps`         | The alarm topic, the alarms, and the `CcfOperations` dashboard. It reads the service stack's exports, so it deploys second                                                                                                                                                                                      |
| `CustomConnectorDestinationFor<Name>` | One destination, its role, and its boundaries. This app doesn't deploy it: a connector project builds it with `buildDestinationApp` and deploys it in the tenant account. The stack name is the destination's name in PascalCase, for example `CustomConnectorDestinationForGitHub` |

`lib/index.ts` exports these constructs:

| Construct                         | Does                                                                                                                                                                                                           |
| --------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `FrameworkValues`                 | The framework's API endpoint, service account, Batch execution role ARN, and destination workflow ARN. `fromConfig()` reads them from the framework's exports, or from literals for a stack in another account |
| `DestinationRole`                 | One destination's role, `CcfDestinationRole-<name>`, under `/ccf/destinations/`, with its own boundary and the boundary of the knowledge base role it creates                                                  |
| `Destination`                     | Registers one destination with `CreateDestination` and waits until it is `ACTIVE`. `fromDestinationId` and `fromDestinationAttributes` reference a destination another stack created                           |
| `ConnectorImage`                  | The `ccf-connectors/` image repository, which only the Batch execution role may pull from, and the connector image, built by AWS CodeBuild in the stack                                                        |
| `CustomConnector`                 | Registers one custom connector, with its source-access role and, when log delivery is on, the log group `/ccf/connectors/<name>` and its log-delivery role                                                     |
| `CustomConnectorDestination`      | A `DestinationRole` and the `Destination` that uses it                                                                                                                                                         |
| `CustomConnectorProject`          | A connector project's `ConnectorImage` and an existing destination; `addCustomConnector` adds one `CustomConnector` per source                                                                                 |
| `CustomConnectorDestinationStack` | The destination stack, with the `DestinationId`, `BucketName`, `DestinationRoleArn`, and for `BEDROCK` the `KnowledgeBaseId` and `DataSourceId` outputs                                                        |

The [Administrator Guide](https://aws-samples.github.io/sample-custom-connector-framework-for-knowledge-bases/administrator-guide/) documents the
framework's configuration in `config/` and the deploy order. The [Developer Guide](https://aws-samples.github.io/sample-custom-connector-framework-for-knowledge-bases/developer-guide/) documents destinations, the roles these constructs
create, and a connector project using them.

## Build and test

Run these from `framework/cdk`:

```bash
make install   # npm ci at the workspace root, and the custom resource Lambdas' Python environment
make check     # tsc, eslint, Jest with cdk-nag, the state machine validation, and the tests of lambdas/connector_ops
```

`make check` needs the API built first (`make -C ../api build`), because the
app reads the packaged OpenAPI document,
`../api/packages/ccf-api-models/src/ccf_api_models/openapi.json`. Synthesis
bundles the Lambda functions in containers; set `CDK_DOCKER=finch` to use
Finch.

## Deploy

The deploy reads the configuration file `CONFIG` names, relative to this
folder:

```bash
make deploy CONFIG=config/default.json
```

Destinations aren't part of this app. A tenant deploys each destination's stack from its own connector project, with the exported `CustomConnectorDestination` construct and `buildDestinationApp`, for example `make -C connectors/github deploy-destination`.
