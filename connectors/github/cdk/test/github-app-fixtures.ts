import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { App, Aws, Stack } from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { GitHubConnectorAppConfig, loadGitHubConnectorAppConfig } from "../lib/common/app-config";
import { ConnectorConfigurationSchema } from "../lib/common/connector-configuration-schema";
import { CONNECTOR_CONFIGURATION_SCHEMA } from "../lib/common/source-paths";
import { buildGitHubConnectorApp } from "../lib/github-connector-app";
import { GitHubCustomConnectorStack } from "../lib/stacks/github-custom-connector-stack";

export type JsonObject = Record<string, unknown>;

/** The stack's account in every synth; `example-small.json`'s framework is in 444455556666. */
export const ACCOUNT = "111122223333";
export const SERVICE_ACCOUNT = "444455556666";
export const REGION = "us-east-1";
export const OWNERS_IN_DEFAULT = 3;

/** cdk.json's feature flags, construct paths in the template metadata, and no Docker bundling. */
const createApp = (): App => {
  const cdkJson = JSON.parse(readFileSync(resolve(__dirname, "..", "cdk.json"), "utf8")) as {
    readonly context: JsonObject;
  };
  return new App({
    context: {
      ...cdkJson.context,
      "aws:cdk:bundling-stacks": [],
      "aws:cdk:enable-path-metadata": true,
    },
  });
};

/** A checked-in configuration file under config/. */
export const loadConfig = (file: string): GitHubConnectorAppConfig =>
  loadGitHubConnectorAppConfig({
    path: resolve(__dirname, "..", "config", file),
    schema: CONNECTOR_SCHEMA,
  });

/** The generated schema every parse in the tests checks against. */
export const CONNECTOR_SCHEMA = ConnectorConfigurationSchema.fromFile(
  CONNECTOR_CONFIGURATION_SCHEMA,
);

export interface SynthesizedApp {
  readonly app: App;
  readonly stack: GitHubCustomConnectorStack;
  readonly connector: Template;
}

export const synthesizeConfig = (config: GitHubConnectorAppConfig): SynthesizedApp => {
  const app = createApp();
  const stack = buildGitHubConnectorApp({ app, config, account: ACCOUNT, region: REGION });
  return { app, stack, connector: Template.fromStack(stack) };
};

export const synthesize = (file: string): SynthesizedApp => synthesizeConfig(loadConfig(file));

export const resourcesOf = (template: Template, type: string): JsonObject[] =>
  Object.values(template.findResources(type));

export const propertiesOf = (resource: JsonObject): JsonObject => resource.Properties as JsonObject;

export const rolesStartingWith = (template: Template, prefix: string): JsonObject[] =>
  resourcesOf(template, "AWS::IAM::Role").filter((role) =>
    String(propertiesOf(role).RoleName).startsWith(prefix),
  );

/** The distinct `Fn::ImportValue` names in a template, sorted. */
export const importedNames = (template: Template): string[] =>
  [
    ...new Set(
      [...JSON.stringify(template.toJSON()).matchAll(/"Fn::ImportValue":"([^"]+)"/g)].map(
        (match) => match[1],
      ),
    ),
  ].sort();

/** The resolved form of an ARN built with the partition token. */
export const partitionArn = (stack: Stack, rest: string): unknown =>
  stack.resolve(`arn:${Aws.PARTITION}:${rest}`);
