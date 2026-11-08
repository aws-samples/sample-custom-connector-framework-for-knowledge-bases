---
description: Deploy a connector project with the Custom Connector Framework's CDK constructs, which build its image and register one custom connector per source.
---

# Deploy a connector

A connector deploys as one [AWS CloudFormation](https://aws.amazon.com/cloudformation/) stack in your account, after its destination is `ACTIVE`. The stack holds one `CustomConnectorProject`, which builds your image, and one custom connector for each part of the source you read, such as one per GitHub organization. The example [GitHub connector's stack](https://github.com/aws-samples/sample-custom-connector-framework-for-knowledge-bases/blob/main/connectors/github/cdk/lib/stacks/github-custom-connector-stack.ts), `GitHubCustomConnector`, is the template a new connector copies.

## What the stack creates

The stack creates these resources in your account:

- `ConnectorImage` creates an [Amazon ECR](https://aws.amazon.com/ecr/) repository named `ccf-connectors/<name>`, whose policy lets only the framework's Batch execution role pull. The CDK uploads the Docker build context, and an arm64 [AWS CodeBuild](https://aws.amazon.com/codebuild/) project builds the image and pushes it with a tag of the source's hash. Each image expires `maxImageAge` after its push. A redeploy pushes a new image only when the source changed, so deploy a source change before the running image expires, or every job fails to pull it.
- `CustomConnector` registers each custom connector with the framework after the image exists. It also creates the custom connector's source-access role and, with log delivery on, its log group and log-delivery role. [Roles in your account](tenant-roles.md) shows their policies.

## Define the stack

This stack deploys a wiki connector with one custom connector:

```typescript
import { App, Duration, RemovalPolicy, Stack } from "aws-cdk-lib";
import { RetentionDays } from "aws-cdk-lib/aws-logs";
import { CustomConnectorProject, Destination, FrameworkValues } from "custom-connector-framework-cdk";

const app = new App();
const stack = new Stack(app, "WikiCustomConnector");
// With no literals, the framework's values come from the CustomConnectorFramework stack's exports.
const framework = FrameworkValues.fromConfig();
const project = new CustomConnectorProject(stack, "Project", {
  framework,
  destination: Destination.fromDestinationId(stack, "Destination", { destinationId: "dst-7c41e9a2b5d0", framework }),
  image: {
    directory: "../..",
    file: "connectors/wiki/backend/Dockerfile",
    repositoryName: "ccf-connectors/wiki",
    maxImageAge: Duration.days(90),
    buildTimeout: Duration.hours(1),
  },
  removalPolicy: RemovalPolicy.RETAIN,
  logRetention: RetentionDays.ONE_MONTH,
});
project.addCustomConnector({
  id: "Engineering",
  name: "wiki-engineering",
  command: ["wiki_connector:WikiConnector"],
  compute: { cpu: 1, memoryMib: 4096, timeout: Duration.hours(2) },
  sourceAccessServices: [],
  deletionTimeout: Duration.hours(2),
});
```

- The image's entry point is `ccf-connector run`, so `command` names only the implementation class.
- `destinationId` is your destination stack's `DestinationId` output. `Destination.fromDestinationId` fails synthesis when it isn't `dst-` and 12 hex characters.
- A custom connector's `name` must be unique among the custom connectors in your account, because its role and log group names come from it.
- Another connector project in the same account needs its own `repositoryName`.
- When the framework runs in another account, pass the framework values to `FrameworkValues.fromConfig` as literals, because CloudFormation exports can't be imported across accounts.
- To let the job read other resources in your account, grant them on the custom connector, as [Read other resources in your account](connector-implementation.md#read-other-resources-in-your-account) describes. `sourceAccessServices` lists the extra service prefixes, such as `dynamodb`, that the roles' boundary allows besides [Amazon S3](https://aws.amazon.com/s3/), [AWS Secrets Manager](https://aws.amazon.com/secrets-manager/), [AWS KMS](https://aws.amazon.com/kms/), and [Amazon CloudWatch Logs](https://aws.amazon.com/cloudwatch/). The construct refuses `iam`, `sts`, and `organizations`.

## Deploy the stack

Deploy it with the [AWS CDK](https://aws.amazon.com/cdk/), in the framework's Region:

```bash
npx cdk deploy WikiCustomConnector
```

The example GitHub connector wraps this in `make -C connectors/github deploy CONFIG=<file>`. See [GitHub connector configuration](../github-connector/configuration.md).

If your connector reads a secret, store your source's credentials in it after the deploy. Then start a job with `StartCustomConnectorJob`, or wait for the custom connector's schedule.

## Delete a connector

Stop any running job first with `StopCustomConnectorJob`, because `DeleteCustomConnector` refuses while a job runs. Then delete the connector's stack. For each custom connector, the framework deletes the schedule, every file under the custom connector's prefix, its document and collection records, and its runtime role. For a `BEDROCK` destination, it then queues an ingestion so the knowledge base drops the documents.

With `RemovalPolicy.RETAIN`, the image repository and the log groups stay. Delete them by hand when you no longer need them.
