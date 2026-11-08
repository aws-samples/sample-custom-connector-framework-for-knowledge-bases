import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { App, Aws, Stack, StackProps } from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { AwsSolutionsChecks } from "cdk-nag";
import { IConstruct } from "constructs";
import { settingsContract } from "../lib/common/contracts";
import { FrameworkValues, FrameworkValuesLiterals } from "../lib/constructs/framework-values";

/** Each unacknowledged AWS Solutions finding under a construct, with the construct paths that break it. */
export const nagViolations = (scope: IConstruct): Record<string, string[]> =>
  Object.fromEntries(
    new AwsSolutionsChecks()
      .validateScope(scope)
      .violations.map((violation) => [
        violation.ruleName,
        violation.violatingResources.map((resource) => resource.constructPath ?? ""),
      ]),
  );

/** Each handler module's environment names, as `settings.json` lists them. */
export const settingsOf = (): Readonly<Record<string, readonly string[]>> =>
  settingsContract().handlers;

export type JsonObject = Record<string, unknown>;

export const SERVICE_ACCOUNT = "444455556666";
export const TENANT_ACCOUNT = "111122223333";
export const DESTINATION_ID = "dst-7c41e9a2b5d0";
export const BUCKET_NAME = `ccf-${DESTINATION_ID}-${TENANT_ACCOUNT}-us-east-1`;
/** The tenant stacks' environment; no Region, so every ARN keeps the partition token. */
export const TENANT_ENV: StackProps["env"] = { account: TENANT_ACCOUNT };
export const IMAGE_DIRECTORY = resolve(__dirname, "fixtures", "connector-image");

export const FRAMEWORK_LITERALS: FrameworkValuesLiterals = {
  endpoint: "https://abcdef1234.execute-api.us-east-1.amazonaws.com/ccf",
  serviceAccount: SERVICE_ACCOUNT,
  batchExecutionRoleArn: `arn:aws:iam::${SERVICE_ACCOUNT}:role/ccf/CcfBatchExecutionRole`,
  destinationWorkflowArn: `arn:aws:states:us-east-1:${SERVICE_ACCOUNT}:stateMachine:CcfDestinationWorkflow`,
};

export const literalFramework = (): FrameworkValues =>
  FrameworkValues.fromConfig(FRAMEWORK_LITERALS);

/** The feature flags `cdk synth` reads from cdk.json, and no Docker bundling. */
export const createApp = (): App => {
  const cdkJson = JSON.parse(readFileSync(resolve(__dirname, "..", "cdk.json"), "utf8")) as {
    readonly context: JsonObject;
  };
  return new App({ context: { ...cdkJson.context, "aws:cdk:bundling-stacks": [] } });
};

export const tenantStack = (id = "Tenant"): Stack =>
  new Stack(createApp(), id, { env: TENANT_ENV });

export const resourcesOf = (template: Template, type: string): JsonObject[] =>
  Object.values(template.findResources(type));

export const propertiesOf = (resource: JsonObject): JsonObject => resource.Properties as JsonObject;

export const statementsOf = (document: unknown): JsonObject[] =>
  (document as { Statement: JsonObject[] }).Statement;

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

/** The logical ID of the only resource of a type. */
export const onlyResourceId = (template: Template, type: string): string => {
  const ids = Object.keys(template.findResources(type));
  if (ids.length !== 1) throw new Error(`Expected one ${type}, found ${ids.length}.`);
  return ids[0];
};
