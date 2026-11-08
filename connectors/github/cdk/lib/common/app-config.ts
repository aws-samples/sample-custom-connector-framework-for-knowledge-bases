import { Duration, RemovalPolicy } from "aws-cdk-lib";
import { RetentionDays } from "aws-cdk-lib/aws-logs";
import {
  ConfigurationError,
  CustomConnectorCompute,
  FrameworkValuesLiterals,
  JsonObject,
  ReadFieldRequest,
  fieldName,
  isJsonObject,
  readBoolean,
  readChoice,
  readConfigFile,
  readInteger,
  readLogRetention,
  readNumber,
  readObject,
  readRemovalPolicy,
  readString,
  readStringList,
  requireValue,
} from "custom-connector-framework-cdk";
import { ConnectorConfigurationSchema, SchemaPart } from "./connector-configuration-schema";

/** The configuration file `cdk synth` and `cdk deploy` read when `--context config` isn't set. */
export const DEFAULT_CONFIG_PATH = "config/default.json";

/** The connector's one stack. */
export enum ConnectorStackName {
  CONNECTOR = "GitHubCustomConnector",
}

/** EventBridge Scheduler cron day-of-week values. */
export enum WeekDay {
  SUNDAY = "SUN",
  MONDAY = "MON",
  TUESDAY = "TUE",
  WEDNESDAY = "WED",
  THURSDAY = "THU",
  FRIDAY = "FRI",
  SATURDAY = "SAT",
}

/** What a source entry names: every repository of an owner, or one repository. */
export enum GitHubSourceType {
  ORG = "ORG",
  REPOSITORY = "REPOSITORY",
}

/** One `sources` entry, with its owner and, for a `REPOSITORY` entry, the repository's name. */
export interface GitHubSource {
  readonly type: GitHubSourceType;
  /** As written in the file, for the redundant-source warning. */
  readonly name: string;
  readonly owner: string;
  readonly repository?: string;
}

/**
 * One owner's custom connector, `github-<owner>`, built from the `sources` entries with that
 * owner and its `owners` settings.
 */
export interface OwnerConnectorConfig {
  readonly owner: string;
  /** The repositories it reads, by name; empty when an `ORG` entry names the owner, which reads them all. */
  readonly repositories: readonly string[];
  readonly scheduleWeekDay: WeekDay;
  /** UTC hour. Owners run at different hours so their Bedrock ingestion jobs do not overlap. */
  readonly scheduleHourUtc: number;
  readonly compute: CustomConnectorCompute;
}

/** The GitHub connector app's deployer configuration, parsed from one JSON file. */
export interface GitHubConnectorAppConfig {
  /** One custom connector per owner the sources name, in the order the owners first appear. */
  readonly connectors: readonly OwnerConnectorConfig[];
  /** `REPOSITORY` entries an `ORG` entry for the same owner already covers; synth warns about each. */
  readonly redundantRepositories: readonly string[];
  /** The backend `GitHubConnectorConfiguration`'s fields other than `owner` and `repositories`. */
  readonly connectorConfiguration: JsonObject;
  /** The arguments the image's entry point runs: the implementation target. */
  readonly command: readonly string[];
  /** How long after its push the connector image expires; redeploy with changed source before then. */
  readonly maxImageAge: Duration;
  /** The retention of the image build's, the providers', and every custom connector's log groups. */
  readonly logRetention: RetentionDays;
  /** Whether each custom connector gets a log group and log-delivery role for its container output. */
  readonly logDelivery: boolean;
  /** What happens to the token secret, the image repository, and the log groups on stack deletion. */
  readonly removalPolicy: RemovalPolicy;
  /** The `DestinationId` output of the `CustomConnectorDestinationFor<Name>` stack the connectors write to. */
  readonly destination: { readonly id: string };
  readonly imageRepositoryName: string;
  /** Service prefixes the source-access roles may call besides the framework's own; `[]` by default. */
  readonly sourceAccessServices: readonly string[];
  readonly costCenter: string;
  readonly team: string;
  /** Present only for a connector in another account than the framework. */
  readonly framework?: FrameworkValuesLiterals;
}

export interface LoadGitHubConnectorAppConfigRequest {
  /** The configuration file path, relative to the working directory or absolute. */
  readonly path: string;
  readonly schema: ConnectorConfigurationSchema;
}

export interface ParseGitHubConnectorAppConfigRequest {
  readonly document: unknown;
  readonly schema: ConnectorConfigurationSchema;
}

interface SourceTypeRule {
  /** The parts of the generated schema the name's pieces must match, in the order the name holds them. */
  readonly parts: readonly SchemaPart[];
}

/** An `ORG` name is an owner; a `REPOSITORY` name is `owner/name`. */
const SOURCE_TYPE_RULES = {
  [GitHubSourceType.ORG]: { parts: [SchemaPart.OWNER] },
  [GitHubSourceType.REPOSITORY]: { parts: [SchemaPart.OWNER, SchemaPart.REPOSITORY_NAME] },
} satisfies Record<GitHubSourceType, SourceTypeRule>;

const MAX_HOUR_UTC = 23;
const CONNECTOR_CONFIGURATION = "connectorConfiguration";

interface SchemaFieldRequest extends ReadFieldRequest {
  readonly schema: ConnectorConfigurationSchema;
}

interface SourceRequest {
  readonly value: unknown;
  readonly field: string;
  readonly schema: ConnectorConfigurationSchema;
}

const readSource = ({ value, field, schema }: SourceRequest): GitHubSource => {
  const object = readObject({ value, field, keys: ["type", "name"] });
  const type = readChoice({
    object,
    parent: field,
    key: "type",
    choices: Object.values(GitHubSourceType),
  });
  const name = readString({ object, parent: field, key: "name" });
  const { parts } = SOURCE_TYPE_RULES[type];
  const pieces = name.split("/");
  const nameField = `${field}.name`;
  if (pieces.length !== parts.length)
    throw new ConfigurationError(
      `"${nameField}" must be ${type === GitHubSourceType.ORG ? "an owner login" : "owner/name"}.`,
    );
  pieces.forEach((piece, index) => {
    schema.check({ part: parts[index], value: piece, field: nameField });
  });
  const repository = pieces.at(1);
  return { type, name, owner: pieces[0], ...(repository === undefined ? {} : { repository }) };
};

const readSources = (request: SchemaFieldRequest): readonly GitHubSource[] => {
  const field = fieldName(request);
  const value = requireValue(request);
  if (!Array.isArray(value) || value.length === 0)
    throw new ConfigurationError(`"${field}" must be a non-empty list.`);
  const items: readonly unknown[] = value;
  const sources = items.map((item, index) =>
    readSource({ value: item, field: `${field}[${index}]`, schema: request.schema }),
  );
  const keys = sources.map(({ type, name }) => `${type} ${name.toLowerCase()}`);
  const repeated = keys.findIndex((key, index) => keys.indexOf(key) !== index);
  if (repeated !== -1)
    throw new ConfigurationError(`"${field}" lists ${sources[repeated].name} more than once.`);
  sources.forEach(({ owner }, index) => {
    const first = sources.findIndex((other) => other.owner.toLowerCase() === owner.toLowerCase());
    if (sources[first].owner !== owner)
      throw new ConfigurationError(
        `"${field}[${index}].name" spells the owner ${owner}, but "${field}[${first}].name" ` +
          `spells it ${sources[first].owner}. GitHub owners are case-insensitive: use one spelling.`,
      );
  });
  return sources;
};

const readCompute = (request: ReadFieldRequest): CustomConnectorCompute => {
  const parent = fieldName(request);
  const object = readObject({
    value: requireValue(request),
    field: parent,
    keys: ["cpu", "memoryMib", "timeoutHours"],
  });
  return {
    cpu: readNumber({ object, parent, key: "cpu", minimum: 0 }),
    memoryMib: readInteger({ object, parent, key: "memoryMib", minimum: 1 }),
    timeout: Duration.hours(readInteger({ object, parent, key: "timeoutHours", minimum: 1 })),
  };
};

interface OwnerConnectorsRequest {
  readonly object: JsonObject;
  readonly sources: readonly GitHubSource[];
  readonly defaultCompute: CustomConnectorCompute;
}

/**
 * Group the sources by owner: an `ORG` entry reads every repository of its owner and makes the
 * owner's `REPOSITORY` entries redundant; otherwise the owner reads only its listed repositories.
 * Every owner needs an `owners` entry, and every `owners` entry needs a source.
 */
const readOwnerConnectors = ({ object, sources, defaultCompute }: OwnerConnectorsRequest) => {
  const owners = [...new Set(sources.map(({ owner }) => owner))];
  const settings = readObject({
    value: requireValue({ object, parent: "", key: "owners" }),
    field: "owners",
    keys: owners,
  });
  const connectors = owners.map((owner): OwnerConnectorConfig => {
    const parent = `owners.${owner}`;
    const ownerObject = readObject({
      value: requireValue({ object: settings, parent: "owners", key: owner }),
      field: parent,
      keys: ["scheduleWeekDay", "scheduleHourUtc", "connectorCompute"],
    });
    const ownSources = sources.filter((source) => source.owner === owner);
    const readsAll = ownSources.some(({ type }) => type === GitHubSourceType.ORG);
    return {
      owner,
      repositories: readsAll ? [] : ownSources.flatMap(({ repository }) => repository ?? []),
      scheduleWeekDay: readChoice({
        object: ownerObject,
        parent,
        key: "scheduleWeekDay",
        choices: Object.values(WeekDay),
      }),
      scheduleHourUtc: readInteger({
        object: ownerObject,
        parent,
        key: "scheduleHourUtc",
        minimum: 0,
        maximum: MAX_HOUR_UTC,
      }),
      compute:
        ownerObject.connectorCompute === undefined
          ? defaultCompute
          : readCompute({ object: ownerObject, parent, key: "connectorCompute" }),
    };
  });
  const redundantRepositories = sources
    .filter(
      ({ type, owner }) =>
        type === GitHubSourceType.REPOSITORY &&
        sources.some((other) => other.type === GitHubSourceType.ORG && other.owner === owner),
    )
    .map(({ name }) => name);
  return { connectors, redundantRepositories };
};

/** The block, checked against the schema with a sample owner, since `owner` and `repositories` are the app's. */
const readConnectorConfiguration = (request: SchemaFieldRequest): JsonObject => {
  const value = requireValue(request);
  const block = readObject({
    value,
    field: CONNECTOR_CONFIGURATION,
    keys: isJsonObject(value)
      ? Object.keys(value).filter((key) => key !== "owner" && key !== "repositories")
      : [],
  });
  request.schema.check({
    part: SchemaPart.CONFIGURATION,
    value: { owner: "owner", repositories: [], ...block },
    field: CONNECTOR_CONFIGURATION,
  });
  return block;
};

const readFrameworkValues = (request: ReadFieldRequest): FrameworkValuesLiterals | undefined => {
  if (request.object[request.key] === undefined) return undefined;
  const parent = fieldName(request);
  const object = readObject({
    value: request.object[request.key],
    field: parent,
    keys: ["endpoint", "serviceAccount", "batchExecutionRoleArn", "destinationWorkflowArn"],
  });
  return {
    endpoint: readString({ object, parent, key: "endpoint" }),
    serviceAccount: readString({ object, parent, key: "serviceAccount" }),
    batchExecutionRoleArn: readString({ object, parent, key: "batchExecutionRoleArn" }),
    destinationWorkflowArn: readString({ object, parent, key: "destinationWorkflowArn" }),
  };
};

const CONFIG_KEYS = [
  "sources",
  "owners",
  "connectorCompute",
  CONNECTOR_CONFIGURATION,
  "command",
  "maxImageAgeDays",
  "logRetentionDays",
  "logDelivery",
  "removalPolicy",
  "destination",
  "imageRepositoryName",
  "sourceAccessServices",
  "costCenter",
  "team",
  "framework",
];

/**
 * Parse the GitHub connector app's configuration document. Values a framework construct checks
 * (the destination ID, the framework values, the image repository, compute, and services) are
 * read for their JSON type only. Throws `ConfigurationError`.
 */
export const parseGitHubConnectorAppConfig = ({
  document,
  schema,
}: ParseGitHubConnectorAppConfigRequest): GitHubConnectorAppConfig => {
  const parent = "";
  const object = readObject({ value: document, field: parent, keys: CONFIG_KEYS });
  const sources = readSources({ object, parent, key: "sources", schema });
  const framework = readFrameworkValues({ object, parent, key: "framework" });
  const destination = readObject({
    value: requireValue({ object, parent, key: "destination" }),
    field: "destination",
    keys: ["id"],
  });
  return {
    ...readOwnerConnectors({
      object,
      sources,
      defaultCompute: readCompute({ object, parent, key: "connectorCompute" }),
    }),
    connectorConfiguration: readConnectorConfiguration({
      object,
      parent,
      key: CONNECTOR_CONFIGURATION,
      schema,
    }),
    command: readStringList({ object, parent, key: "command" }),
    maxImageAge: Duration.days(readInteger({ object, parent, key: "maxImageAgeDays", minimum: 1 })),
    logRetention: readLogRetention({ object, parent, key: "logRetentionDays" }),
    logDelivery: readBoolean({ object, parent, key: "logDelivery" }),
    removalPolicy: readRemovalPolicy({ object, parent, key: "removalPolicy" }),
    destination: { id: readString({ object: destination, parent: "destination", key: "id" }) },
    imageRepositoryName: readString({ object, parent, key: "imageRepositoryName" }),
    sourceAccessServices: readStringList({ object, parent, key: "sourceAccessServices" }),
    costCenter: readString({ object, parent, key: "costCenter" }),
    team: readString({ object, parent, key: "team" }),
    ...(framework === undefined ? {} : { framework }),
  };
};

/** Read and parse a configuration file. The error names the file and the field. */
export const loadGitHubConnectorAppConfig = ({
  path,
  schema,
}: LoadGitHubConnectorAppConfigRequest): GitHubConnectorAppConfig =>
  readConfigFile({
    path,
    parse: (document) => parseGitHubConnectorAppConfig({ document, schema }),
  });
