import { resolve } from "node:path";
import { Stack } from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { FrameworkAppConfig, loadFrameworkAppConfig } from "../lib/common/app-config";
import { loadDestinationAppConfig } from "../lib/common/destination-config";
import { FrameworkExport, FrameworkStackName } from "../lib/common/framework-exports";
import { FRAMEWORK_ACKNOWLEDGMENTS } from "../lib/common/nag-suppressions";
import { FrameworkAppStacks, buildDestinationApp, buildFrameworkApp } from "../lib/framework-app";
import { createApp, importedNames, nagViolations } from "./construct-fixtures";

const ACCOUNT = "444455556666";
const REGION = "us-east-1";

type JsonObject = Record<string, unknown>;

/** A checked-in configuration file under config/. */
const loadConfig = (file: string): FrameworkAppConfig =>
  loadFrameworkAppConfig({
    path: resolve(__dirname, "..", "config", file),
    serviceAccount: ACCOUNT,
  });

const synthesize = (config: FrameworkAppConfig): FrameworkAppStacks =>
  buildFrameworkApp({ app: createApp(), account: ACCOUNT, region: REGION, config });

const exportNames = (stack: Stack): string[] =>
  Object.values(Template.fromStack(stack).toJSON().Outputs as Record<string, JsonObject>)
    .map((output) => (output.Export as JsonObject | undefined)?.Name)
    .filter((name): name is string => name !== undefined);

describe("framework app", () => {
  let defaults: FrameworkAppConfig;
  let disposable: FrameworkAppConfig;
  let disposableStacks: FrameworkAppStacks;
  let defaultStacks: FrameworkAppStacks;

  beforeAll(() => {
    defaults = loadConfig("default.json");
    disposable = loadConfig("example-disposable.json");
    disposableStacks = synthesize(disposable);
    defaultStacks = synthesize(defaults);
  });

  it("names its two stacks CustomConnectorFramework and CustomConnectorFrameworkOps", () => {
    expect([defaultStacks.service.stackName, defaultStacks.ops.stackName]).toEqual([
      FrameworkStackName.SERVICE,
      FrameworkStackName.OPS,
    ]);
  });

  it.each([
    ["example-disposable.json", (): FrameworkAppStacks => disposableStacks],
    ["default.json", (): FrameworkAppStacks => defaultStacks],
  ])("has no unacknowledged cdk-nag findings with %s", (_, stacks) => {
    const { service, ops } = stacks();
    for (const stack of [service, ops])
      expect({ stack: stack.stackName, violations: nagViolations(stack) }).toEqual({
        stack: stack.stackName,
        violations: {},
      });
  });

  it("exports every value in the contract between the two stacks", () => {
    const exported = [
      ...exportNames(defaultStacks.service),
      ...exportNames(defaultStacks.ops),
    ].sort();
    expect(exported).toEqual(Object.values(FrameworkExport).sort());
  });

  it("the Ops stack imports only names the service stack exports", () => {
    const imports = importedNames(Template.fromStack(defaultStacks.ops));
    expect(imports.length).toBeGreaterThan(0);
    for (const name of imports) expect(exportNames(defaultStacks.service)).toContain(name);
  });

  it("deploys the Ops stack after the service stack whose exports it imports", () => {
    expect(defaultStacks.ops.dependencies).toContain(defaultStacks.service);
  });

  it.each([
    ["example-disposable.json", 100, (): FrameworkAppStacks => disposableStacks],
    ["default.json", 15, (): FrameworkAppStacks => defaultStacks],
  ])(
    "writes deletionProtectionThreshold from %s into the destination workflow",
    (_, threshold, stacks) => {
      const [machine] = Object.values(
        Template.fromStack(stacks().service).findResources("AWS::StepFunctions::StateMachine", {
          Properties: { StateMachineName: "CcfDestinationWorkflow" },
        }),
      );
      expect(JSON.stringify(machine.Properties.DefinitionString)).toContain(
        `\\"DeletionProtectionThreshold\\":${threshold}`,
      );
    },
  );

  it("retains nothing with removalPolicy DESTROY, so a teardown leaves no resources behind", () => {
    for (const stack of [disposableStacks.service, disposableStacks.ops]) {
      const resources = Template.fromStack(stack).toJSON().Resources as Record<string, JsonObject>;
      const retained = Object.entries(resources)
        .filter(([, resource]) => (resource.DeletionPolicy ?? "Delete") !== "Delete")
        .map(([id]) => id);
      expect({ stack: stack.stackName, retained }).toEqual({
        stack: stack.stackName,
        retained: [],
      });
    }
  });

  it.each([
    ["example-disposable.json", 7, (): FrameworkAppStacks => disposableStacks],
    ["default.json", 30, (): FrameworkAppStacks => defaultStacks],
  ])("keeps every framework log group for logRetentionDays with %s", (_, days, stacks) => {
    const logGroups = Object.values(
      Template.fromStack(stacks().service).findResources("AWS::Logs::LogGroup"),
    );
    expect(logGroups.length).toBeGreaterThan(0);
    for (const logGroup of logGroups)
      expect((logGroup.Properties as JsonObject).RetentionInDays).toBe(days);
  });

  it.each([
    ["example-disposable.json", "ccf-dev", (): FrameworkAppStacks => disposableStacks],
    ["default.json", "unassigned", (): FrameworkAppStacks => defaultStacks],
  ])(
    "tags both stacks with costCenter and no environment or provider tag with %s",
    (_, costCenter, stacks) => {
      for (const stack of [stacks().service, stacks().ops]) {
        const serialized = JSON.stringify(Template.fromStack(stack).toJSON());
        expect(serialized).toContain(`{"Key":"cost-center","Value":"${costCenter}"}`);
        expect(serialized).not.toContain('"Key":"environment"');
        expect(serialized).not.toContain('"Key":"provider"');
      }
    },
  );

  it("lets the configured tenant accounts call the API", () => {
    const apiPrincipals = (stack: Stack): string => {
      const [api] = Object.values(
        Template.fromStack(stack).findResources("AWS::ApiGateway::RestApi"),
      );
      const policy = (api.Properties as { Policy: { Statement: { Principal?: unknown }[] } })
        .Policy;
      return JSON.stringify(policy.Statement.map((statement) => statement.Principal));
    };
    expect(disposable.allowedTenantAccountIds).toEqual(["111122223333"]);
    expect(apiPrincipals(disposableStacks.service)).toContain(":iam::111122223333:root");
    expect(apiPrincipals(defaultStacks.service)).not.toContain("111122223333");
  });
});

describe("destination app", () => {
  const app = createApp();
  const stack = buildDestinationApp({
    app,
    account: ACCOUNT,
    region: REGION,
    config: loadDestinationAppConfig({
      path: resolve(__dirname, "..", "config", "destination-github.json"),
    }),
  });

  it("holds exactly one stack, CustomConnectorDestinationFor<Name>", () => {
    expect(
      app.node.children.filter((child) => child instanceof Stack).map((child) => child.node.id),
    ).toEqual(["CustomConnectorDestinationForGitHub"]);
    expect(stack.stackName).toBe("CustomConnectorDestinationForGitHub");
  });
});

describe("stack-level cdk-nag acknowledgments", () => {
  it("given the service stack's list, when read, then none acknowledges Resource::* for every role", () => {
    expect(FRAMEWORK_ACKNOWLEDGMENTS.flatMap(({ findings }) => findings ?? [])).not.toContain(
      "Resource::*",
    );
  });
});
