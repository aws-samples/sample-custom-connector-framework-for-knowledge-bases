import { App, Environment, Stack, Tags, Validations } from "aws-cdk-lib";
import { AwsSolutionsChecks } from "cdk-nag";
import { FrameworkAppConfig } from "./common/app-config";
import { ProjectName, SERVICE_SETTINGS } from "./common/config";
import { DestinationAppConfig } from "./common/destination-config";
import { FrameworkStackName } from "./common/framework-exports";
import { acknowledgeNagRules, FRAMEWORK_ACKNOWLEDGMENTS } from "./common/nag-suppressions";
import { FrameworkValues } from "./constructs/framework-values";
import {
  CustomConnectorDestinationStack,
  destinationStackName,
} from "./stacks/custom-connector-destination-stack";
import { CustomConnectorFrameworkOpsStack } from "./stacks/custom-connector-framework-ops-stack";
import { CustomConnectorFrameworkStack } from "./stacks/custom-connector-framework-stack";

export interface FrameworkAppRequest {
  readonly app: App;
  readonly account: string;
  readonly region: string;
  readonly config: FrameworkAppConfig;
}

export interface FrameworkAppStacks {
  readonly service: CustomConnectorFrameworkStack;
  readonly ops: CustomConnectorFrameworkOpsStack;
}

export interface AppPoliciesRequest {
  readonly app: App;
  readonly stacks: readonly Stack[];
  readonly costCenter: string;
}

/** Tag every stack with the project and cost center, and run cdk-nag over the app. */
export const applyAppPolicies = ({ app, stacks, costCenter }: AppPoliciesRequest): void => {
  for (const stack of stacks) {
    Tags.of(stack).add("project", ProjectName.CCF);
    Tags.of(stack).add("cost-center", costCenter);
  }
  Validations.of(app).addPlugins(new AwsSolutionsChecks(app, { verbose: true }));
};

/**
 * Add `CustomConnectorFramework` and `CustomConnectorFrameworkOps` to the app. The Ops stack
 * reads the service stack's exports by name, so it depends on the service stack and deploys after it.
 */
export const buildFrameworkApp = (request: FrameworkAppRequest): FrameworkAppStacks => {
  const { app, account, region, config } = request;
  const { costCenter, alarms, ...serviceConfig } = config;
  const env: Environment = { account, region };
  const service = new CustomConnectorFrameworkStack(app, FrameworkStackName.SERVICE, {
    ...serviceConfig,
    stackName: FrameworkStackName.SERVICE,
    description:
      "Custom Connector Framework: the API, its state, job compute, and the destination and connector deletion workflows.",
    projectName: ProjectName.CCF,
    region,
    env,
  });
  acknowledgeNagRules({ scopes: [service], acknowledgments: FRAMEWORK_ACKNOWLEDGMENTS });
  const ops = new CustomConnectorFrameworkOpsStack(app, FrameworkStackName.OPS, {
    stackName: FrameworkStackName.OPS,
    description: "Custom Connector Framework alarms and the SNS topic they notify.",
    alarms,
    env,
  });
  ops.addStackDependency(service, "The ops stack imports the service stack's exports by name.");
  applyAppPolicies({ app, stacks: [service, ops], costCenter });
  return { service, ops };
};

export interface DestinationAppRequest {
  readonly app: App;
  readonly account: string;
  readonly region: string;
  readonly config: DestinationAppConfig;
}

/**
 * Add one `CustomConnectorDestinationFor<Name>` stack to the app. Without `framework` values in
 * its configuration, it reads the framework's exports, so it deploys in the service account and
 * Region after the framework.
 */
export const buildDestinationApp = (
  request: DestinationAppRequest,
): CustomConnectorDestinationStack => {
  const { app, account, region, config } = request;
  const stackName = destinationStackName(config.name);
  const framework = FrameworkValues.fromConfig(config.framework);
  const stack = new CustomConnectorDestinationStack(app, stackName, {
    stackName,
    description: `The Custom Connector Framework destination ${config.name}: its role, boundary, bucket, and knowledge base.`,
    framework,
    destinationName: config.name,
    destinationType: config.type,
    accessControlEnabled: config.accessControlEnabled,
    metadataFields: config.metadataFields,
    removalPolicy: config.removalPolicy,
    logRetention: config.logRetention,
    providerTimeout: SERVICE_SETTINGS.destinationProviderTimeout,
    env: { account, region },
  });
  applyAppPolicies({ app, stacks: [stack], costCenter: config.costCenter });
  return stack;
};
