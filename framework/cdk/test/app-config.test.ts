import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { RemovalPolicy } from "aws-cdk-lib";
import { RetentionDays } from "aws-cdk-lib/aws-logs";
import {
  ConfigurationError,
  FrameworkAppConfig,
  loadFrameworkAppConfig,
  parseFrameworkAppConfig,
  withDefaults,
} from "../lib/common/app-config";

const SERVICE_ACCOUNT = "444455556666";
const TENANT_ACCOUNT = "111122223333";

type JsonObject = Record<string, unknown>;

/** What `config/default.json` parses to. */
const DEFAULT_CONFIG: FrameworkAppConfig = {
  removalPolicy: RemovalPolicy.RETAIN,
  allowedTenantAccountIds: [],
  apiThrottling: { rateLimit: 100, burstLimit: 200 },
  deletionProtectionThreshold: 15,
  logRetention: RetentionDays.ONE_MONTH,
  costCenter: "unassigned",
  jobCompute: { maxVcpus: 256, natGateways: 1 },
  alarms: { apiLatencyMs: 5000, apiServerErrorPercent: 5 },
};

/** Each bounded nested integer key, one value below its range, and one above. */
const OUT_OF_RANGE: readonly [string, string, number, number][] = [
  ["jobCompute", "maxVcpus", 0, 10_001],
  ["jobCompute", "natGateways", 0, 3],
  ["alarms", "apiLatencyMs", 0, 29_001],
  ["alarms", "apiServerErrorPercent", 0, 101],
];

const completeDocument = (): JsonObject => ({
  removalPolicy: "RETAIN",
  allowedTenantAccountIds: [],
  apiThrottling: { rateLimit: 100, burstLimit: 200 },
  deletionProtectionThreshold: 15,
  logRetentionDays: 30,
  costCenter: "unassigned",
  jobCompute: { maxVcpus: 256, natGateways: 1 },
  alarms: { apiLatencyMs: 5000, apiServerErrorPercent: 5 },
});

const parse = (document: unknown) =>
  parseFrameworkAppConfig({ document, serviceAccount: SERVICE_ACCOUNT });

const without = (key: string): JsonObject =>
  Object.fromEntries(Object.entries(completeDocument()).filter(([name]) => name !== key));

interface OverrideFileRequest {
  readonly overrides: unknown;
  readonly check: (path: string) => void;
}

/** Write `overrides` to a temporary deployer file, hand its path to `check`, then delete it. */
const withOverrideFile = ({ overrides, check }: OverrideFileRequest): void => {
  const directory = mkdtempSync(join(tmpdir(), "ccf-config-"));
  try {
    const path = join(directory, "deployer.json");
    writeFileSync(path, JSON.stringify(overrides));
    check(path);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
};

const load = (path: string): FrameworkAppConfig =>
  loadFrameworkAppConfig({ path, serviceAccount: SERVICE_ACCOUNT });

describe("framework configuration loader", () => {
  it("loads config/default.json with data-retaining values", () => {
    expect(load(resolve(__dirname, "..", "config", "default.json"))).toEqual(DEFAULT_CONFIG);
  });

  it("lays config/example-disposable.json over the defaults", () => {
    expect(load(resolve(__dirname, "..", "config", "example-disposable.json"))).toEqual({
      ...DEFAULT_CONFIG,
      removalPolicy: RemovalPolicy.DESTROY,
      allowedTenantAccountIds: [TENANT_ACCOUNT],
      apiThrottling: { rateLimit: 20, burstLimit: 40 },
      deletionProtectionThreshold: 100,
      logRetention: RetentionDays.ONE_WEEK,
      costCenter: "ccf-dev",
    });
  });

  it("keeps every default when the deployer file is empty", () => {
    withOverrideFile({
      overrides: {},
      check: (path) => {
        expect(load(path)).toEqual(DEFAULT_CONFIG);
      },
    });
  });

  it("keeps the default of a nested key the deployer file leaves out", () => {
    withOverrideFile({
      overrides: { jobCompute: { natGateways: 2 } },
      check: (path) => {
        expect(load(path).jobCompute).toEqual({ maxVcpus: 256, natGateways: 2 });
      },
    });
  });

  it("names the deployer file and the key in an error", () => {
    withOverrideFile({
      overrides: { env: "test" },
      check: (path) => {
        expect(() => load(path)).toThrow(
          `Invalid configuration file ${path}: "env" is not a known configuration key.`,
        );
      },
    });
  });

  it("names a nested unknown key in the deployer file", () => {
    withOverrideFile({
      overrides: { alarms: { apiLatency: 100 } },
      check: (path) => {
        expect(() => load(path)).toThrow('"alarms.apiLatency" is not a known configuration key.');
      },
    });
  });

  it("names the file when it can't be read", () => {
    const path = join(tmpdir(), "ccf-config-missing", "none.json");
    expect(() => load(path)).toThrow(`Unable to read the configuration file ${path}.`);
  });
});

describe("withDefaults", () => {
  it("merges a nested object key by key and replaces a list", () => {
    expect(
      withDefaults({
        defaults: { list: ["a"], nested: { kept: 1, changed: 2 }, scalar: "x" },
        overrides: { list: ["b"], nested: { changed: 3 } },
      }),
    ).toEqual({ list: ["b"], nested: { kept: 1, changed: 3 }, scalar: "x" });
  });

  it("rejects a deployer document that isn't a JSON object", () => {
    expect(() => withDefaults({ defaults: {}, overrides: [] })).toThrow(
      new ConfigurationError("The document must be a JSON object."),
    );
  });
});

describe("framework configuration parser", () => {
  it.each(Object.keys(completeDocument()))(
    "rejects a merged document without %s, naming the field",
    (key) => {
      expect(() => parse(without(key))).toThrow(
        new ConfigurationError(`"${key}" is required and missing.`),
      );
    },
  );

  it.each(OUT_OF_RANGE)(
    "rejects %s.%s below and above its range, naming the field and the range",
    (group, key, tooLow, tooHigh) => {
      const document = completeDocument();
      const groupDefaults = document[group] as JsonObject;
      for (const value of [tooLow, tooHigh, 1.5])
        expect(() => parse({ ...document, [group]: { ...groupDefaults, [key]: value } })).toThrow(
          new RegExp(`^"${group}\\.${key}" must be an integer from \\d+ to \\d+\\.$`),
        );
    },
  );

  it("rejects apiThrottling without burstLimit, naming the nested field", () => {
    expect(() => parse({ ...completeDocument(), apiThrottling: { rateLimit: 1 } })).toThrow(
      '"apiThrottling.burstLimit" is required and missing.',
    );
  });

  it.each([
    [{ env: "prod" }, "env"],
    [{ lambdaMemoryMb: 1024 }, "lambdaMemoryMb"],
    [{ apiThrottling: { rateLimit: 1, burstLimit: 2, period: 1 } }, "apiThrottling.period"],
  ])("rejects an unknown key, naming it: %j", (extra, field) => {
    expect(() => parse({ ...completeDocument(), ...extra })).toThrow(
      `"${field}" is not a known configuration key.`,
    );
  });

  it.each([
    [{ removalPolicy: "SNAPSHOT" }, "removalPolicy"],
    [{ removalPolicy: "retain" }, "removalPolicy"],
    [{ allowedTenantAccountIds: "111122223333" }, "allowedTenantAccountIds"],
    [{ allowedTenantAccountIds: ["11112222333"] }, "allowedTenantAccountIds[0]"],
    [{ apiThrottling: { rateLimit: "100", burstLimit: 200 } }, "apiThrottling.rateLimit"],
    [{ apiThrottling: { rateLimit: 0, burstLimit: 200 } }, "apiThrottling.rateLimit"],
    [{ apiThrottling: { rateLimit: 100, burstLimit: 0 } }, "apiThrottling.burstLimit"],
    [{ deletionProtectionThreshold: 101 }, "deletionProtectionThreshold"],
    [{ deletionProtectionThreshold: -1 }, "deletionProtectionThreshold"],
    [{ deletionProtectionThreshold: 1.5 }, "deletionProtectionThreshold"],
    [{ logRetentionDays: 31 }, "logRetentionDays"],
    [{ logRetentionDays: 9999 }, "logRetentionDays"],
    [{ costCenter: "   " }, "costCenter"],
    [{ costCenter: "" }, "costCenter"],
    [{ costCenter: 7 }, "costCenter"],
    [{ jobCompute: 2 }, "jobCompute"],
  ])("rejects a wrong type or value, naming the field: %j", (override, field) => {
    expect(() => parse({ ...completeDocument(), ...override })).toThrow(`"${field}"`);
  });

  it("accepts a deletionProtectionThreshold of 0", () => {
    expect(
      parse({ ...completeDocument(), deletionProtectionThreshold: 0 }).deletionProtectionThreshold,
    ).toBe(0);
  });

  it("rejects the service account among the tenant accounts", () => {
    expect(() =>
      parse({ ...completeDocument(), allowedTenantAccountIds: [TENANT_ACCOUNT, SERVICE_ACCOUNT] }),
    ).toThrow('"allowedTenantAccountIds[1]" is the service account');
  });

  it("removes repeated tenant accounts", () => {
    expect(
      parse({ ...completeDocument(), allowedTenantAccountIds: [TENANT_ACCOUNT, TENANT_ACCOUNT] })
        .allowedTenantAccountIds,
    ).toEqual([TENANT_ACCOUNT]);
  });

  it("rejects a document that isn't a JSON object", () => {
    expect(() => parse([])).toThrow("The document must be a JSON object.");
  });
});
