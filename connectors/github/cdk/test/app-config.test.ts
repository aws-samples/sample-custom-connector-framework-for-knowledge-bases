import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { RemovalPolicy } from "aws-cdk-lib";
import { ConfigurationError, isJsonObject } from "custom-connector-framework-cdk";
import {
  GitHubConnectorAppConfig,
  GitHubSourceType,
  loadGitHubConnectorAppConfig,
  parseGitHubConnectorAppConfig,
} from "../lib/common/app-config";
import { CONNECTOR_SCHEMA } from "./github-app-fixtures";

type JsonObject = Record<string, unknown>;

const configPath = (file: string): string => resolve(__dirname, "..", "config", file);

/** config/default.json as JSON, so each test can change one field. */
const defaultDocument = (): JsonObject => {
  const document: unknown = JSON.parse(readFileSync(configPath("default.json"), "utf8"));
  return isJsonObject(document) ? { ...document } : {};
};

const parse = (document: unknown): GitHubConnectorAppConfig =>
  parseGitHubConnectorAppConfig({ document, schema: CONNECTOR_SCHEMA });

const withBlock = (override: JsonObject): JsonObject => {
  const document = defaultDocument();
  const block = document.connectorConfiguration;
  return {
    ...document,
    connectorConfiguration: { ...(isJsonObject(block) ? block : {}), ...override },
  };
};

const SCHEDULE = { scheduleWeekDay: "SUN", scheduleHourUtc: 2 };
const withSources = (sources: JsonObject[], owners: JsonObject): JsonObject => ({
  ...defaultDocument(),
  sources,
  owners,
});
const ownersOf = (config: GitHubConnectorAppConfig): JsonObject[] =>
  config.connectors.map(({ owner, repositories }) => ({ owner, repositories }));

describe("GitHub connector configuration parser", () => {
  it("parses config/default.json", () => {
    const config = loadGitHubConnectorAppConfig({
      path: configPath("default.json"),
      schema: CONNECTOR_SCHEMA,
    });
    expect(ownersOf(config)).toEqual([
      { owner: "aws-samples", repositories: [] },
      { owner: "awslabs", repositories: [] },
      { owner: "aws", repositories: [] },
    ]);
  });

  it("keeps data by default: removalPolicy RETAIN and log delivery on", () => {
    const config = loadGitHubConnectorAppConfig({
      path: configPath("default.json"),
      schema: CONNECTOR_SCHEMA,
    });
    expect([config.removalPolicy, config.logDelivery]).toEqual([RemovalPolicy.RETAIN, true]);
  });

  it("rejects a document without sources, naming the field", () => {
    const document = Object.fromEntries(
      Object.entries(defaultDocument()).filter(([key]) => key !== "sources"),
    );
    expect(() => parse(document)).toThrow(
      new ConfigurationError('"sources" is required and missing.'),
    );
  });

  it("rejects an unknown top-level key, naming it", () => {
    expect(() => parse({ ...defaultDocument(), env: "prod" })).toThrow(
      new ConfigurationError('"env" is not a known configuration key.'),
    );
  });

  it.each([
    [
      {
        github: {
          request_interval_seconds: 2,
          request_timeout_seconds: 30,
          retry_attempts: 5,
          retry_backoff_seconds: 2,
          max_rate_limit_wait_seconds: 0,
        },
      },
      '"connectorConfiguration.github.max_rate_limit_wait_seconds" must be > 0.',
    ],
    [
      { unknown_key: 1 },
      '"connectorConfiguration.unknown_key" must NOT have additional properties.',
    ],
    [{ owner: "aws" }, '"connectorConfiguration.owner" is not a known configuration key.'],
  ])("rejects a connector configuration value the model rejects: %j", (override, message) => {
    expect(() => parse(withBlock(override))).toThrow(new ConfigurationError(message));
  });

  it("accepts a fractional request interval, which the model types as a number", () => {
    const config = parse(
      withBlock({
        github: {
          request_interval_seconds: 0.5,
          request_timeout_seconds: 30,
          retry_attempts: 5,
          retry_backoff_seconds: 2,
          max_rate_limit_wait_seconds: 3900,
        },
      }),
    );
    expect(config.connectorConfiguration.github).toMatchObject({ request_interval_seconds: 0.5 });
  });

  it.each([
    [
      { type: "ORG", name: "aws fork:true" },
      '"sources[0].name" must match pattern "^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$".',
    ],
    [{ type: "REPOSITORY", name: "awslabs/a/b" }, '"sources[0].name" must be owner/name.'],
    [{ type: "ORG", name: "aws/samples" }, '"sources[0].name" must be an owner login.'],
  ])("rejects a source name the model's patterns reject: %j", (source, message) => {
    expect(() => parse(withSources([source], { aws: SCHEDULE }))).toThrow(
      new ConfigurationError(message),
    );
  });

  it("rejects a repeated source", () => {
    const source = { type: "ORG", name: "aws" };
    expect(() => parse({ ...defaultDocument(), sources: [source, source] })).toThrow(
      new ConfigurationError('"sources" lists aws more than once.'),
    );
  });

  it("makes one connector per ORG source that reads every repository of its owner", () => {
    const config = parse(
      withSources([{ type: "ORG", name: "aws-samples" }], { "aws-samples": SCHEDULE }),
    );
    expect(ownersOf(config)).toEqual([{ owner: "aws-samples", repositories: [] }]);
    expect(config.redundantRepositories).toEqual([]);
  });

  it("groups REPOSITORY sources with one owner into one connector that reads only those repositories", () => {
    const config = parse(
      withSources(
        [
          { type: "REPOSITORY", name: "awslabs/git-secrets" },
          { type: "REPOSITORY", name: "awslabs/aws-shell" },
        ],
        { awslabs: SCHEDULE },
      ),
    );
    expect(ownersOf(config)).toEqual([
      { owner: "awslabs", repositories: ["git-secrets", "aws-shell"] },
    ]);
  });

  it("makes one connector per owner when sources mix owners, each with its own schedule and compute", () => {
    const config = parse(
      withSources(
        [
          { type: "ORG", name: "aws-samples" },
          { type: "REPOSITORY", name: "awslabs/git-secrets" },
        ],
        {
          "aws-samples": SCHEDULE,
          awslabs: {
            scheduleWeekDay: "MON",
            scheduleHourUtc: 5,
            connectorCompute: { cpu: 1, memoryMib: 2048, timeoutHours: 1 },
          },
        },
      ),
    );
    const [samples, labs] = config.connectors;
    expect(samples).toMatchObject({ owner: "aws-samples", repositories: [], scheduleHourUtc: 2 });
    expect(samples.compute.memoryMib).toBe(8192);
    expect(labs).toMatchObject({
      owner: "awslabs",
      repositories: ["git-secrets"],
      scheduleWeekDay: "MON",
    });
    expect(labs.compute.memoryMib).toBe(2048);
  });

  it("lets an ORG source cover the REPOSITORY sources of its owner and lists them as redundant", () => {
    const config = parse(
      withSources(
        [
          { type: "REPOSITORY", name: "awslabs/git-secrets" },
          { type: GitHubSourceType.ORG, name: "awslabs" },
        ],
        { awslabs: SCHEDULE },
      ),
    );
    expect(ownersOf(config)).toEqual([{ owner: "awslabs", repositories: [] }]);
    expect(config.redundantRepositories).toEqual(["awslabs/git-secrets"]);
  });

  it("rejects an owner without an owners entry", () => {
    expect(() => parse(withSources([{ type: "ORG", name: "aws" }], {}))).toThrow(
      new ConfigurationError('"owners.aws" is required and missing.'),
    );
  });

  it("rejects an owners entry without a source", () => {
    expect(() =>
      parse(withSources([{ type: "ORG", name: "aws" }], { aws: SCHEDULE, awslabs: SCHEDULE })),
    ).toThrow(new ConfigurationError('"owners.awslabs" is not a known configuration key.'));
  });

  it("rejects two sources whose owners differ only in case, naming the second source", () => {
    expect(() =>
      parse(
        withSources(
          [
            { type: "ORG", name: "AWSLabs" },
            { type: "REPOSITORY", name: "awslabs/git-secrets" },
          ],
          { AWSLabs: SCHEDULE },
        ),
      ),
    ).toThrow(
      new ConfigurationError(
        '"sources[1].name" spells the owner awslabs, but "sources[0].name" spells it AWSLabs. ' +
          "GitHub owners are case-insensitive: use one spelling.",
      ),
    );
  });

  it("names the file in a load error", () => {
    const path = configPath("does-not-exist.json");
    expect(() => loadGitHubConnectorAppConfig({ path, schema: CONNECTOR_SCHEMA })).toThrow(
      `Unable to read the configuration file ${path}.`,
    );
  });
});
