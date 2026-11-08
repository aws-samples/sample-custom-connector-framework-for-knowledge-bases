import { readFileSync } from "node:fs";
import { resolve } from "node:path";

/**
 * The one CDK reader of what the API build generates: the packaged `openapi.json` and the contract
 * files the backend reads too (`tables.json`, `metrics.json`, `settings.json`, `runtime_role_tags.json`,
 * `job_container.json`). Each file is read once; a missing or malformed file fails synthesis naming it.
 */

const PACKAGE_DIRECTORY = resolve(
  __dirname,
  "../../../api/packages/ccf-api-models/src/ccf_api_models",
);
const OPENAPI_FILE = resolve(PACKAGE_DIRECTORY, "openapi.json");
const CONTRACTS_DIRECTORY = resolve(PACKAGE_DIRECTORY, "contracts");
const CALLERS_EXTENSION = "x-ccf-callers";
const RUNTIME_CALLER = "RUNTIME";
const PATH_LABEL = /\{[^}]+\}/g;

export interface OpenApiSchema {
  readonly type?: string;
  readonly enum?: readonly string[];
  readonly pattern?: string;
  readonly minLength?: number;
  readonly maxLength?: number;
  readonly maxItems?: number;
  readonly minimum?: number;
  readonly maximum?: number;
  readonly properties?: Readonly<Record<string, OpenApiSchema>>;
  readonly [extension: string]: unknown;
}

export interface OpenApiOperation {
  readonly operationId: string;
  readonly [CALLERS_EXTENSION]?: readonly string[];
  readonly [member: string]: unknown;
}

export interface OpenApiDocument {
  readonly paths: Readonly<Record<string, Readonly<Record<string, OpenApiOperation>>>>;
  readonly components: { readonly schemas: Readonly<Record<string, OpenApiSchema>> };
  readonly [member: string]: unknown;
}

/** An HTTP method and a path; in an `execute-api` route, `*` stands for each `{label}`. */
export interface ApiRoute {
  readonly method: string;
  readonly path: string;
}

/** One member of a generated schema, for a limit the model inlines rather than naming. */
export interface SchemaMember {
  readonly schema: string;
  readonly member: string;
}

export interface KeyAttribute {
  readonly name: string;
  readonly type: "STRING" | "NUMBER";
}

export interface IndexContract {
  readonly partitionKey: KeyAttribute;
  readonly sortKey: KeyAttribute | null;
  /** The generated summary schema whose members the index holds besides the keys. */
  readonly projection: string;
}

export interface TableContract {
  readonly name: string;
  readonly partitionKey: KeyAttribute;
  readonly sortKey: KeyAttribute | null;
  readonly binaryAttributes: readonly string[];
  readonly timeToLiveAttribute: string | null;
  readonly indexes: Readonly<Record<string, IndexContract>>;
}

export interface TablesContract {
  readonly destinations: TableContract;
  readonly connectors: TableContract;
  readonly jobs: TableContract;
  readonly collections: TableContract;
  readonly documents: TableContract;
  readonly idempotency: TableContract;
}

export interface MetricContract {
  readonly name: string;
  readonly unit: string;
  /** Each set's dimension keys, which `dimensions` maps to their published names. */
  readonly dimensionSets: readonly (readonly string[])[];
}

export interface MetricsContract {
  readonly namespace: string;
  readonly dimensions: Readonly<Record<string, string>>;
  readonly metrics: Readonly<Record<string, MetricContract>>;
}

export interface SettingsContract {
  /** Each handler module's environment variable names. */
  readonly handlers: Readonly<Record<string, readonly string[]>>;
}

/** The tag keys on a runtime role, which the backend writes and the runtime role boundary reads. */
export interface RuntimeRoleTagsContract {
  readonly destinationId: string;
  readonly connectorId: string;
  readonly tenantAccount: string;
  readonly sourceAccessRole: string;
  readonly logDeliveryRole: string;
}

/** A job's two containers, the connector container's variables, and the exit codes it ends with. */
export interface JobContainerContract {
  readonly containers: { readonly connector: string; readonly logRouter: string };
  /** camelCase key, such as `jobId`, to the environment variable name. */
  readonly variables: Readonly<Record<string, string>>;
  readonly exitCodes: Readonly<Record<string, number>>;
}

const cache = new Map<string, unknown>();

/** Parse a generated JSON file once; the caller names the shape it holds. */
const readJson = (file: string): unknown => {
  if (!cache.has(file)) {
    try {
      cache.set(file, JSON.parse(readFileSync(file, "utf8")));
    } catch (error: unknown) {
      throw new Error(`Unable to read ${file}. Run 'make -C framework/api build' first.`, {
        cause: error,
      });
    }
  }
  return cache.get(file);
};

export const openApi = (): OpenApiDocument => readJson(OPENAPI_FILE) as OpenApiDocument;
export const tablesContract = (): TablesContract =>
  readJson(resolve(CONTRACTS_DIRECTORY, "tables.json")) as TablesContract;
export const metricsContract = (): MetricsContract =>
  readJson(resolve(CONTRACTS_DIRECTORY, "metrics.json")) as MetricsContract;
export const settingsContract = (): SettingsContract =>
  readJson(resolve(CONTRACTS_DIRECTORY, "settings.json")) as SettingsContract;
export const runtimeRoleTagsContract = (): RuntimeRoleTagsContract =>
  readJson(resolve(CONTRACTS_DIRECTORY, "runtime_role_tags.json")) as RuntimeRoleTagsContract;
export const jobContainerContract = (): JobContainerContract =>
  readJson(resolve(CONTRACTS_DIRECTORY, "job_container.json")) as JobContainerContract;

/** A framework metric's published name, such as `JobsCompleted` for `JOBS_COMPLETED`. */
export const frameworkMetricName = (key: string): string => {
  const { metrics } = metricsContract();
  if (!Object.hasOwn(metrics, key)) throw new Error(`metrics.json has no metric ${key}.`);
  return metrics[key].name;
};

/** A framework metric dimension's published name, such as `Status` for `STATUS`. */
export const frameworkDimensionName = (key: string): string => {
  const { dimensions } = metricsContract();
  if (!Object.hasOwn(dimensions, key)) throw new Error(`metrics.json has no dimension ${key}.`);
  return dimensions[key];
};

const operations = (): {
  readonly method: string;
  readonly path: string;
  readonly operation: OpenApiOperation;
}[] =>
  Object.entries(openApi().paths).flatMap(([path, pathItem]) =>
    Object.entries(pathItem).map(([method, operation]) => ({
      method: method.toUpperCase(),
      path,
      operation,
    })),
  );

const byPathThenMethod = (first: ApiRoute, second: ApiRoute): number =>
  first.path.localeCompare(second.path) || first.method.localeCompare(second.method);

/** The route templates of the operations whose `x-ccf-callers` holds `RUNTIME`, sorted by path and method. */
export const runtimeRoutes = (): readonly ApiRoute[] => {
  const routes = operations()
    .filter(({ operation }) => (operation[CALLERS_EXTENSION] ?? []).includes(RUNTIME_CALLER))
    .map(({ method, path }) => ({ method, path }));
  if (routes.length === 0)
    throw new Error(`${OPENAPI_FILE} admits ${RUNTIME_CALLER} on no operation.`);
  return routes.sort(byPathThenMethod);
};

/** One `execute-api` route per named operation, each `{label}` as `*`, in the order named. */
export const operationRoutes = (names: readonly string[]): readonly ApiRoute[] =>
  names.map((name) => {
    const found = operations().find(({ operation }) => operation.operationId === name);
    if (found === undefined) throw new Error(`${OPENAPI_FILE} has no operation ${name}.`);
    return { method: found.method, path: found.path.replace(PATH_LABEL, "*") };
  });

/** The path prefix every operation shares, such as `/api/v1/destinations`. */
export const apiPathPrefix = (): string => {
  const paths = Object.keys(openApi().paths).map((path) => path.split("/"));
  const first = paths.at(0);
  if (first === undefined) throw new Error(`${OPENAPI_FILE} has no paths.`);
  const shared: string[] = [];
  for (const [index, segment] of first.entries()) {
    if (!paths.every((segments) => segments[index] === segment)) break;
    shared.push(segment);
  }
  return shared.join("/");
};

const schema = (name: string): OpenApiSchema => {
  const { schemas } = openApi().components;
  if (!Object.hasOwn(schemas, name)) throw new Error(`${OPENAPI_FILE} has no schema ${name}.`);
  return schemas[name];
};

const member = ({ schema: schemaName, member: memberName }: SchemaMember): OpenApiSchema => {
  const properties = schema(schemaName).properties ?? {};
  if (!Object.hasOwn(properties, memberName))
    throw new Error(`${OPENAPI_FILE} has no member ${memberName} in ${schemaName}.`);
  return properties[memberName];
};

/** The values of a generated string enum, such as `JobStatus`. */
export const enumValues = (name: string): readonly string[] => {
  const values = schema(name).enum;
  if (values === undefined) throw new Error(`${OPENAPI_FILE}'s ${name} is not an enum.`);
  return values;
};

/** One value of a generated enum; synthesis fails when the model drops it. */
export const enumMember = (request: SchemaMember): string => {
  if (!enumValues(request.schema).includes(request.member))
    throw new Error(`${OPENAPI_FILE}'s ${request.schema} has no member ${request.member}.`);
  return request.member;
};

/** The pattern of a generated string schema, such as `DestinationId`. */
export const stringPattern = (name: string): RegExp => {
  const pattern = schema(name).pattern;
  if (pattern === undefined) throw new Error(`${OPENAPI_FILE}'s ${name} has no pattern.`);
  return new RegExp(pattern);
};

/** A string member's maximum length, such as `Destination.status_reason`. */
export const maxLength = (request: SchemaMember): number => {
  const length = member(request).maxLength;
  if (length === undefined)
    throw new Error(`${OPENAPI_FILE}'s ${request.schema}.${request.member} has no maxLength.`);
  return length;
};

/** A list member's maximum number of items, such as `Destination.metadata_fields`. */
export const maxItems = (request: SchemaMember): number => {
  const items = member(request).maxItems;
  if (items === undefined)
    throw new Error(`${OPENAPI_FILE}'s ${request.schema}.${request.member} has no maxItems.`);
  return items;
};

/** A number member's range, such as `ContainerProperties.timeout`. */
export const numberRange = (
  request: SchemaMember,
): { readonly minimum: number; readonly maximum: number } => {
  const { minimum, maximum } = member(request);
  if (minimum === undefined || maximum === undefined)
    throw new Error(`${OPENAPI_FILE}'s ${request.schema}.${request.member} has no range.`);
  return { minimum, maximum };
};

/** The members of a summary schema an index projects: every property that isn't one of the given keys. */
export const summaryAttributes = (request: {
  readonly schema: string;
  readonly keys: readonly string[];
}): readonly string[] =>
  Object.keys(schema(request.schema).properties ?? {}).filter(
    (name) => !request.keys.includes(name),
  );
