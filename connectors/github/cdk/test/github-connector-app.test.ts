import { execFileSync } from "node:child_process";
import { resolve } from "node:path";
import { Stack } from "aws-cdk-lib";
import { Annotations, Match, Template } from "aws-cdk-lib/assertions";
import { AwsSolutionsChecks } from "cdk-nag";
import { ConnectorStackName, GitHubConnectorAppConfig } from "../lib/common/app-config";
import { createConnectorConfiguration } from "../lib/common/owner-connectors";
import {
  JsonObject,
  OWNERS_IN_DEFAULT,
  SERVICE_ACCOUNT,
  SynthesizedApp,
  importedNames,
  loadConfig,
  propertiesOf,
  resourcesOf,
  rolesStartingWith,
  synthesize,
  synthesizeConfig,
} from "./github-app-fixtures";

const CONNECTOR_PACKAGE = resolve(__dirname, "..", "..", "backend");
const IAM_TYPES = ["AWS::IAM::Role", "AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"];
/** The construct paths the framework's constructs create IAM resources under. */
const FRAMEWORK_CONSTRUCT_PATHS = [
  /^GitHubCustomConnector\/Project\//,
  /^GitHubCustomConnector\/CcfConnectorRegistrationProvider\//,
  /^GitHubCustomConnector\/CcfImageBuildProvider\//,
];

const logGroups = (template: Template, prefix: string): JsonObject[] =>
  resourcesOf(template, "AWS::Logs::LogGroup").filter((group) =>
    String(propertiesOf(group).LogGroupName).startsWith(prefix),
  );

describe("GitHub connector app", () => {
  let small: SynthesizedApp;
  let defaults: SynthesizedApp;

  beforeAll(() => {
    small = synthesize("example-small.json");
    defaults = synthesize("default.json");
  });

  it("synthesizes exactly one stack, GitHubCustomConnector", () => {
    for (const { app } of [defaults, small])
      expect(
        app.node.children.filter((child) => Stack.isStack(child)).map((child) => child.node.id),
      ).toEqual([ConnectorStackName.CONNECTOR]);
  });

  it("builds the image into ccf-connectors/github and creates no destination", () => {
    for (const { connector } of [defaults, small]) {
      expect(
        resourcesOf(connector, "AWS::ECR::Repository").map(
          (repository) => propertiesOf(repository).RepositoryName,
        ),
      ).toEqual(["ccf-connectors/github"]);
      expect(resourcesOf(connector, "Custom::CcfConnectorImage")).toHaveLength(1);
      expect(resourcesOf(connector, "Custom::CcfDestination")).toEqual([]);
      expect(rolesStartingWith(connector, "CcfDestinationRole")).toEqual([]);
    }
  });

  it("registers every custom connector in the configured destination", () => {
    for (const [{ connector }, destinationId] of [
      [defaults, "dst-0123456789ab"],
      [small, "dst-7c41e9a2b5d0"],
    ] as const)
      for (const registration of resourcesOf(connector, "Custom::CcfCustomConnector"))
        expect(JSON.stringify(propertiesOf(registration).Request)).toContain(
          `\\"destination_id\\":\\"${destinationId}\\"`,
        );
  });

  it("has no IAM role or policy outside the framework's constructs", () => {
    for (const { connector } of [defaults, small])
      for (const type of IAM_TYPES)
        for (const resource of resourcesOf(connector, type)) {
          const path = String((resource.Metadata as JsonObject)["aws:cdk:path"]);

          expect({
            path,
            owned: FRAMEWORK_CONSTRUCT_PATHS.some((owner) => owner.test(path)),
          }).toEqual({
            path,
            owned: true,
          });
        }
  });

  it("has no unacknowledged cdk-nag findings with either configuration", () => {
    for (const { stack } of [defaults, small]) {
      const rules = new AwsSolutionsChecks()
        .validateScope(stack)
        .violations.map(({ ruleName }) => ruleName);
      expect({ stack: stack.stackName, rules }).toEqual({ stack: stack.stackName, rules: [] });
    }
  });

  it("has no CloudWatch alarm or dashboard with either configuration", () => {
    for (const { connector } of [defaults, small]) {
      expect(resourcesOf(connector, "AWS::CloudWatch::Alarm")).toEqual([]);
      expect(resourcesOf(connector, "AWS::CloudWatch::Dashboard")).toEqual([]);
    }
  });

  it("outputs each owner's custom connector ID and the token secret's ARN", () => {
    expect(Object.keys(defaults.connector.findOutputs("*")).sort()).toEqual([
      "ConnectorIdaws",
      "ConnectorIdawslabs",
      "ConnectorIdawssamples",
      "GitHubTokenSecretArn",
    ]);
  });

  it("keeps every log group's records for logRetentionDays: 30 by default and 7 in example-small", () => {
    for (const [{ connector }, days] of [
      [defaults, 30],
      [small, 7],
    ] as const)
      for (const group of resourcesOf(connector, "AWS::Logs::LogGroup"))
        expect(propertiesOf(group).RetentionInDays).toBe(days);
  });

  it("registers each configuration as one JSON string in the request", () => {
    for (const { connector } of [defaults, small])
      for (const registration of resourcesOf(connector, "Custom::CcfCustomConnector"))
        expect(JSON.stringify(propertiesOf(registration).Request)).toMatch(
          /connector_configuration\\":\\"\{\\\\\\"owner\\\\\\":.*max_repositories/,
        );
  });
});

describe("GitHub connector app with default.json in the service account", () => {
  let app: SynthesizedApp;

  beforeAll(() => {
    app = synthesize("default.json");
  });

  it("registers three custom connectors, each with a log group and a log-delivery role", () => {
    expect(resourcesOf(app.connector, "Custom::CcfCustomConnector")).toHaveLength(
      OWNERS_IN_DEFAULT,
    );
    expect(logGroups(app.connector, "/ccf/connectors/github-")).toHaveLength(OWNERS_IN_DEFAULT);
    expect(rolesStartingWith(app.connector, "CcfLogDeliveryRole-github-")).toHaveLength(
      OWNERS_IN_DEFAULT,
    );
  });

  it("tags the stack with the configured cost-center and team", () => {
    const tagged = synthesizeConfig({
      ...loadConfig("default.json"),
      costCenter: "cc-42",
      team: "search",
    });
    const serialized = JSON.stringify(tagged.connector.toJSON());
    expect(serialized).toContain('{"Key":"cost-center","Value":"cc-42"}');
    expect(serialized).toContain('{"Key":"team","Value":"search"}');
  });

  it("warns at synth about a REPOSITORY source an ORG source for its owner already covers", () => {
    const config = loadConfig("default.json");
    const covered = synthesizeConfig({ ...config, redundantRepositories: ["awslabs/git-secrets"] });
    Annotations.fromStack(covered.stack).hasWarning(
      "*",
      Match.stringLikeRegexp("REPOSITORY source awslabs/git-secrets is redundant"),
    );
  });
});

describe("GitHub connector app with example-small.json in another account than the framework", () => {
  let app: SynthesizedApp;

  beforeAll(() => {
    app = synthesize("example-small.json");
  });

  it("registers one custom connector with no log group and no log-delivery role", () => {
    expect(resourcesOf(app.connector, "Custom::CcfCustomConnector")).toHaveLength(1);
    expect(logGroups(app.connector, "/ccf/connectors/")).toEqual([]);
    expect(rolesStartingWith(app.connector, "CcfLogDeliveryRole-")).toEqual([]);
  });

  it("puts the framework values in the constructs' properties and imports nothing", () => {
    const [repository] = resourcesOf(app.connector, "AWS::ECR::Repository");

    expect(JSON.stringify(propertiesOf(repository).RepositoryPolicyText)).toContain(
      `arn:aws:iam::${SERVICE_ACCOUNT}:role/ccf/CcfBatchExecutionRole`,
    );
    expect(JSON.stringify(app.connector.toJSON())).toContain(
      `:execute-api:us-east-1:${SERVICE_ACCOUNT}:abcdef1234/*/`,
    );
    expect(importedNames(app.connector)).toEqual([]);
  });
});

describe("connector configuration", () => {
  /** Validates the configuration JSON in the backend's locked environment with its Pydantic model. */
  const validate = (configuration: string): void => {
    execFileSync(
      "uv",
      [
        "run",
        "--locked",
        "--quiet",
        "--project",
        CONNECTOR_PACKAGE,
        "python",
        "-c",
        "import sys; from common.configuration import GitHubConnectorConfiguration as C; " +
          "C.model_validate_json(sys.stdin.read())",
      ],
      {
        input: configuration,
        stdio: ["pipe", "pipe", "pipe"],
        env: { ...process.env, PYTHONPATH: resolve(CONNECTOR_PACKAGE, "src") },
      },
    );
  };
  const configurations = (config: GitHubConnectorAppConfig): string[] =>
    config.connectors.map((owner) => createConnectorConfiguration({ config, owner }));

  it.each(["example-small.json", "default.json"])(
    "registers every owner with the configured command, from %s",
    (file) => {
      const config = loadConfig(file);
      const app = synthesize(file);
      const registrations = resourcesOf(app.connector, "Custom::CcfCustomConnector");
      expect(registrations).toHaveLength(config.connectors.length);
      for (const registration of registrations)
        expect(JSON.stringify(propertiesOf(registration).Request)).toContain(
          JSON.stringify(JSON.stringify(config.command)).slice(1, -1),
        );
    },
  );

  it.each(["example-small.json", "default.json"])(
    "validates against the connector's Pydantic model for every owner in %s",
    (file) => {
      for (const configuration of configurations(loadConfig(file)))
        expect(() => {
          validate(configuration);
        }).not.toThrow();
    },
  );
});
