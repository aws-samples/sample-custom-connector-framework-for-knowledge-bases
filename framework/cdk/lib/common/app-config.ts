import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { RemovalPolicy } from "aws-cdk-lib";
import { RetentionDays } from "aws-cdk-lib/aws-logs";

/** The configuration file `cdk synth` and `cdk deploy` read when `--context config` isn't set. */
export const DEFAULT_CONFIG_PATH = "config/default.json";

/** The `removalPolicy` values a configuration file accepts. */
export enum RemovalPolicySetting {
  RETAIN = "RETAIN",
  DESTROY = "DESTROY",
}

const REMOVAL_POLICIES = {
  [RemovalPolicySetting.RETAIN]: RemovalPolicy.RETAIN,
  [RemovalPolicySetting.DESTROY]: RemovalPolicy.DESTROY,
} satisfies Record<RemovalPolicySetting, RemovalPolicy>;

const AWS_ACCOUNT_ID_PATTERN = /^\d{12}$/;
/** CloudWatch Logs retention periods. `INFINITE` isn't one: it means no retention policy. */
const LOG_RETENTION_DAYS: readonly number[] = Object.values(RetentionDays).filter(
  (value): value is number => typeof value === "number" && value !== RetentionDays.INFINITE,
);

/** The bounds each integer key accepts: the AWS service's own limits. */
const INTEGER_BOUNDS = {
  deletionProtectionThreshold: { minimum: 0, maximum: 100 },
  maxVcpus: { minimum: 1, maximum: 10_000 },
  natGateways: { minimum: 1, maximum: 2 },
  apiLatencyMs: { minimum: 1, maximum: 29_000 },
  apiServerErrorPercent: { minimum: 1, maximum: 100 },
} as const satisfies Record<string, IntegerBounds>;

/** The CCF API's stage-wide request limits. */
export interface ApiThrottlingConfig {
  /** Steady-state requests per second across all callers. */
  readonly rateLimit: number;
  /** Requests API Gateway admits at once above the steady rate. */
  readonly burstLimit: number;
}

/** The Batch compute connector jobs run on. */
export interface JobComputeConfig {
  /** The Fargate vCPU cap across all running connector jobs. */
  readonly maxVcpus: number;
  /** The job VPC's NAT gateways: 1 is cheaper, 2 (one per zone) keeps jobs running when a zone fails. */
  readonly natGateways: number;
}

/** The thresholds of the Ops stack's API alarms. */
export interface AlarmsConfig {
  /** The API's p99 latency above which its alarm fires. */
  readonly apiLatencyMs: number;
  /** The API's 5xx percentage above which its alarm fires. */
  readonly apiServerErrorPercent: number;
}

/** The framework app's deployer configuration: `config/default.json` with a deployer file's keys over it. */
export interface FrameworkAppConfig {
  /** What happens to tables and log groups when their stack is deleted. */
  readonly removalPolicy: RemovalPolicy;
  /** Tenant accounts besides the service account that may call the API. */
  readonly allowedTenantAccountIds: readonly string[];
  readonly apiThrottling: ApiThrottlingConfig;
  /** The largest percentage of indexed documents one Bedrock ingestion job may delete. */
  readonly deletionProtectionThreshold: number;
  /** The retention of every framework log group. */
  readonly logRetention: RetentionDays;
  /** The value of the `cost-center` tag on every stack. */
  readonly costCenter: string;
  readonly jobCompute: JobComputeConfig;
  readonly alarms: AlarmsConfig;
}

export interface ParseFrameworkAppConfigRequest {
  /** The parsed JSON document. */
  readonly document: unknown;
  /** The account the framework deploys to, which `allowedTenantAccountIds` must not repeat. */
  readonly serviceAccount: string;
}

export interface LoadFrameworkAppConfigRequest {
  /** The configuration file path, relative to the working directory or absolute. */
  readonly path: string;
  readonly serviceAccount: string;
}

/** A configuration value is missing, unknown, or invalid. The message names the field. */
export class ConfigurationError extends Error {
  /** The message alone, so a loader that adds the file name doesn't repeat the class name. */
  override toString(): string {
    return this.message;
  }
}

export type JsonObject = Readonly<Record<string, unknown>>;

export interface ReadObjectRequest {
  readonly value: unknown;
  readonly field: string;
  readonly keys: readonly string[];
}

export interface ReadFieldRequest {
  readonly object: JsonObject;
  readonly parent: string;
  readonly key: string;
}

export interface IntegerBounds {
  readonly minimum: number;
  /** Unset accepts any integer of at least `minimum`. */
  readonly maximum?: number;
}

export type ReadIntegerRequest = ReadFieldRequest & IntegerBounds;

export interface ReadChoiceRequest<T extends string> extends ReadFieldRequest {
  readonly choices: readonly T[];
}

export interface ReadConfigFileRequest<T> {
  /** The configuration file path, relative to the working directory or absolute. */
  readonly path: string;
  readonly parse: (document: unknown) => T;
}

export const fieldName = ({ parent, key }: Omit<ReadFieldRequest, "object">): string =>
  parent.length === 0 ? key : `${parent}.${key}`;

export const isJsonObject = (value: unknown): value is JsonObject =>
  typeof value === "object" && value !== null && !Array.isArray(value);

/** Read a JSON object and reject any key it doesn't expect. `field` is "" for the document itself. */
export const readObject = ({ value, field, keys }: ReadObjectRequest): JsonObject => {
  if (!isJsonObject(value))
    throw new ConfigurationError(
      field.length === 0
        ? "The document must be a JSON object."
        : `"${field}" must be a JSON object.`,
    );
  const unknownKey = Object.keys(value).find((key) => !keys.includes(key));
  if (unknownKey !== undefined)
    throw new ConfigurationError(
      `"${fieldName({ parent: field, key: unknownKey })}" is not a known configuration key.`,
    );
  return value;
};

export const requireValue = (request: ReadFieldRequest): unknown => {
  const value = request.object[request.key];
  if (value === undefined)
    throw new ConfigurationError(`"${fieldName(request)}" is required and missing.`);
  return value;
};

export const readString = (request: ReadFieldRequest): string => {
  const value = requireValue(request);
  if (typeof value !== "string" || value.trim().length === 0)
    throw new ConfigurationError(`"${fieldName(request)}" must be a non-empty string.`);
  return value;
};

/** The `cost-center` tag value: any non-empty string. */
export const readCostCenter = readString;

export const readBoolean = (request: ReadFieldRequest): boolean => {
  const value = requireValue(request);
  if (typeof value !== "boolean")
    throw new ConfigurationError(`"${fieldName(request)}" must be true or false.`);
  return value;
};

export const readNumber = (request: ReadFieldRequest & { readonly minimum: number }): number => {
  const value = requireValue(request);
  if (typeof value !== "number" || !(value >= request.minimum))
    throw new ConfigurationError(
      `"${fieldName(request)}" must be a number of at least ${request.minimum}.`,
    );
  return value;
};

export const readInteger = (request: ReadIntegerRequest): number => {
  const value = requireValue(request);
  const { minimum, maximum } = request;
  const inRange =
    typeof value === "number" &&
    Number.isInteger(value) &&
    value >= minimum &&
    (maximum === undefined || value <= maximum);
  if (!inRange)
    throw new ConfigurationError(
      maximum === undefined
        ? `"${fieldName(request)}" must be an integer of at least ${minimum}.`
        : `"${fieldName(request)}" must be an integer from ${minimum} to ${maximum}.`,
    );
  return value;
};

export const readChoice = <T extends string>(request: ReadChoiceRequest<T>): T => {
  const value = requireValue(request);
  const choice = request.choices.find((candidate) => candidate === value);
  if (choice === undefined)
    throw new ConfigurationError(
      `"${fieldName(request)}" must be one of ${request.choices.join(", ")}.`,
    );
  return choice;
};

export const readStringList = (request: ReadFieldRequest): readonly string[] => {
  const field = fieldName(request);
  const value = requireValue(request);
  if (!Array.isArray(value)) throw new ConfigurationError(`"${field}" must be a list of strings.`);
  const items: readonly unknown[] = value;
  return items.map((item, index) => {
    if (typeof item !== "string" || item.length === 0)
      throw new ConfigurationError(`"${field}[${index}]" must be a non-empty string.`);
    return item;
  });
};

export const readRemovalPolicy = (request: ReadFieldRequest): RemovalPolicy =>
  REMOVAL_POLICIES[readChoice({ ...request, choices: Object.values(RemovalPolicySetting) })];

export const readLogRetention = (request: ReadFieldRequest): RetentionDays => {
  const value = requireValue(request);
  const retention = Object.values(RetentionDays).find(
    (days): days is RetentionDays => days === value && days !== RetentionDays.INFINITE,
  );
  if (retention === undefined)
    throw new ConfigurationError(
      `"${fieldName(request)}" must be one of CloudWatch Logs' retention periods: ${LOG_RETENTION_DAYS.join(", ")}.`,
    );
  return retention;
};

/** Read a JSON configuration file and parse it; the error names the file and, from `parse`, the field. */
export const readConfigFile = <T>({ path, parse }: ReadConfigFileRequest<T>): T => {
  const absolutePath = resolve(path);
  let document: unknown;
  try {
    document = JSON.parse(readFileSync(absolutePath, "utf8"));
  } catch (error: unknown) {
    throw new ConfigurationError(`Unable to read the configuration file ${absolutePath}.`, {
      cause: error,
    });
  }
  try {
    return parse(document);
  } catch (error: unknown) {
    throw new ConfigurationError(`Invalid configuration file ${absolutePath}: ${String(error)}`, {
      cause: error,
    });
  }
};

const readDeletionProtectionThreshold = (request: ReadFieldRequest): number => {
  const bounds = INTEGER_BOUNDS.deletionProtectionThreshold;
  const threshold = readInteger({ ...request, minimum: bounds.minimum });
  if (threshold > bounds.maximum)
    throw new ConfigurationError(
      `"${fieldName(request)}" must be a percentage from 0 to ${bounds.maximum}.`,
    );
  return threshold;
};

const readApiThrottling = (request: ReadFieldRequest): ApiThrottlingConfig => {
  const field = fieldName(request);
  const object = readObject({
    value: requireValue(request),
    field,
    keys: ["rateLimit", "burstLimit"],
  });
  return {
    rateLimit: readInteger({ object, parent: field, key: "rateLimit", minimum: 1 }),
    burstLimit: readInteger({ object, parent: field, key: "burstLimit", minimum: 1 }),
  };
};

const readJobCompute = (request: ReadFieldRequest): JobComputeConfig => {
  const field = fieldName(request);
  const object = readObject({
    value: requireValue(request),
    field,
    keys: ["maxVcpus", "natGateways"],
  });
  return {
    maxVcpus: readInteger({ object, parent: field, key: "maxVcpus", ...INTEGER_BOUNDS.maxVcpus }),
    natGateways: readInteger({
      object,
      parent: field,
      key: "natGateways",
      ...INTEGER_BOUNDS.natGateways,
    }),
  };
};

const readAlarms = (request: ReadFieldRequest): AlarmsConfig => {
  const field = fieldName(request);
  const object = readObject({
    value: requireValue(request),
    field,
    keys: ["apiLatencyMs", "apiServerErrorPercent"],
  });
  return {
    apiLatencyMs: readInteger({
      object,
      parent: field,
      key: "apiLatencyMs",
      ...INTEGER_BOUNDS.apiLatencyMs,
    }),
    apiServerErrorPercent: readInteger({
      object,
      parent: field,
      key: "apiServerErrorPercent",
      ...INTEGER_BOUNDS.apiServerErrorPercent,
    }),
  };
};

const readTenantAccountIds = (
  request: ReadFieldRequest & { readonly serviceAccount: string },
): readonly string[] => {
  const field = fieldName(request);
  const value = requireValue(request);
  if (!Array.isArray(value))
    throw new ConfigurationError(`"${field}" must be a list of 12-digit AWS account IDs.`);
  const accountIds = value.map((accountId: unknown, index) => {
    if (typeof accountId !== "string" || !AWS_ACCOUNT_ID_PATTERN.test(accountId))
      throw new ConfigurationError(`"${field}[${index}]" must be a 12-digit AWS account ID.`);
    if (accountId === request.serviceAccount)
      throw new ConfigurationError(
        `"${field}[${index}]" is the service account, which may always call the API.`,
      );
    return accountId;
  });
  return [...new Set(accountIds)];
};

const CONFIG_KEYS = [
  "removalPolicy",
  "allowedTenantAccountIds",
  "apiThrottling",
  "deletionProtectionThreshold",
  "logRetentionDays",
  "costCenter",
  "jobCompute",
  "alarms",
];

/** Parse a complete configuration document: every key present. Throws {@link ConfigurationError}. */
export const parseFrameworkAppConfig = ({
  document,
  serviceAccount,
}: ParseFrameworkAppConfigRequest): FrameworkAppConfig => {
  const parent = "";
  const object = readObject({ value: document, field: parent, keys: CONFIG_KEYS });
  return {
    removalPolicy: readRemovalPolicy({ object, parent, key: "removalPolicy" }),
    allowedTenantAccountIds: readTenantAccountIds({
      object,
      parent,
      key: "allowedTenantAccountIds",
      serviceAccount,
    }),
    apiThrottling: readApiThrottling({ object, parent, key: "apiThrottling" }),
    deletionProtectionThreshold: readDeletionProtectionThreshold({
      object,
      parent,
      key: "deletionProtectionThreshold",
    }),
    logRetention: readLogRetention({ object, parent, key: "logRetentionDays" }),
    costCenter: readCostCenter({ object, parent, key: "costCenter" }),
    jobCompute: readJobCompute({ object, parent, key: "jobCompute" }),
    alarms: readAlarms({ object, parent, key: "alarms" }),
  };
};

export interface WithDefaultsRequest {
  readonly defaults: JsonObject;
  /** The deployer file's document: only the keys it changes. */
  readonly overrides: unknown;
}

/**
 * Lay `overrides` over `defaults`. A nested object merges key by key; a list or a scalar replaces
 * the default. A key `defaults` doesn't have is kept, so the parser reports it as unknown.
 */
export const withDefaults = ({ defaults, overrides }: WithDefaultsRequest): JsonObject => {
  if (!isJsonObject(overrides)) throw new ConfigurationError("The document must be a JSON object.");
  return Object.fromEntries([
    ...Object.entries(defaults),
    ...Object.entries(overrides).map(([key, override]): [string, unknown] => {
      const fallback = defaults[key];
      return [
        key,
        isJsonObject(fallback) && isJsonObject(override)
          ? withDefaults({ defaults: fallback, overrides: override })
          : override,
      ];
    }),
  ]);
};

/** The shipped file that holds every key's default. */
const DEFAULTS_FILE = resolve(__dirname, "..", "..", DEFAULT_CONFIG_PATH);

/**
 * Read `config/default.json`, lay the deployer file at `path` over it, and parse the result.
 * A key the deployer file leaves out keeps its default. The error names the file and the field.
 */
export const loadFrameworkAppConfig = ({
  path,
  serviceAccount,
}: LoadFrameworkAppConfigRequest): FrameworkAppConfig => {
  const defaults = readConfigFile({
    path: DEFAULTS_FILE,
    parse: (document) => {
      parseFrameworkAppConfig({ document, serviceAccount });
      return readObject({ value: document, field: "", keys: CONFIG_KEYS });
    },
  });
  return readConfigFile({
    path,
    parse: (overrides) =>
      parseFrameworkAppConfig({ document: withDefaults({ defaults, overrides }), serviceAccount }),
  });
};
