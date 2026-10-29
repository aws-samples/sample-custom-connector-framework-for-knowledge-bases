#!/usr/bin/env node
import { Annotations, App, Aspects, Tags } from "aws-cdk-lib";
import { AwsSolutionsChecks, NagSuppressions } from "cdk-nag";
import {
  EnvironmentName,
  ProjectName,
  createConstructId,
  createStackName,
  resolveAllowedClientAccountIds,
  resolveProvider,
} from "./common/config";
import { NAG_SUPPRESSIONS } from "./common/nag-suppressions";
import { KnowledgeBase } from "./construct-groups/knowledge-base";
import { CustomConnectorFrameworkStack } from "./stacks/custom-connector-framework-stack";
import { synthSampleStack } from "./sample-app";

const DEFAULT_REGION = "us-east-1";

const app = new App();
const account = process.env.CDK_DEFAULT_ACCOUNT;
const region = process.env.CDK_DEFAULT_REGION ?? DEFAULT_REGION;

// A `--context sample=web-crawler|gitlab|github` synth targets a sample connector
// stack that deploys against an already-deployed framework, instead of the
// framework stack itself. Kept in its own module so app.ts stays focused.
if (synthSampleStack(app, account, region)) {
  app.synth();
} else {
  synthFrameworkStack();
}

function synthFrameworkStack(): void {
  const environmentContext =
    (app.node.tryGetContext("env") as string | undefined) ?? EnvironmentName.DEV;
  const environmentName =
    Object.values(EnvironmentName).find(
      (name) => name.toLowerCase() === environmentContext.toLowerCase(),
    ) ?? EnvironmentName.DEV;

  const costCenterContext = app.node.tryGetContext("costCenter") as string | undefined;
  if (environmentName === EnvironmentName.PROD && costCenterContext === undefined) {
    throw new Error('Missing required context value "costCenter" for a production synth.');
  }
  const costCenter = costCenterContext ?? "unassigned";
  const provider = resolveProvider(app.node.tryGetContext("provider") as string | undefined);
  const allowedClientAccountIds = resolveAllowedClientAccountIds({
    values: app.node.tryGetContext("allowedClientAccountIds"),
    frameworkAccount: account ?? "",
  });

  const stack = new CustomConnectorFrameworkStack(
    app,
    createConstructId(createStackName(ProjectName.CCF, environmentName)),
    {
      projectName: ProjectName.CCF,
      environmentName,
      retainResources: environmentName === EnvironmentName.PROD,
      account: account ?? "",
      region,
      allowedClientAccountIds,
      provider,
      embeddingModelId: KnowledgeBase.resolveEmbeddingModelId(
        app.node.tryGetContext("embeddingModelId") as string | undefined,
      ),
      vectorDimension: KnowledgeBase.resolveVectorDimension(
        app.node.tryGetContext("vectorDimension") as string | undefined,
      ),
      env: { account, region },
      description:
        "Custom Connector Framework: state/orchestration backend, S3 ingestion bucket, optional Bedrock KB.",
    },
  );

  Tags.of(stack).add("project", ProjectName.CCF);
  Tags.of(stack).add("environment", environmentName);
  Tags.of(stack).add("cost-center", costCenter);
  Tags.of(stack).add("provider", provider);

  Aspects.of(app).add(new AwsSolutionsChecks({ verbose: true }));
  if (NAG_SUPPRESSIONS.length > 0) {
    NagSuppressions.addStackSuppressions(stack, [...NAG_SUPPRESSIONS]);
  }

  if (account === undefined) {
    Annotations.of(stack).addInfo(
      "No CDK_DEFAULT_ACCOUNT set — synthesizing an environment-agnostic template.",
    );
  }

  app.synth();
}
