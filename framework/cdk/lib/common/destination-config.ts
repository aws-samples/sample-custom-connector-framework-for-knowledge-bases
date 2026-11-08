import { RemovalPolicy } from "aws-cdk-lib";
import { RetentionDays } from "aws-cdk-lib/aws-logs";
import { FrameworkValuesLiterals } from "../constructs/framework-values";
import type { MetadataFieldProps } from "../constructs/destination";
import {
  ConfigurationError,
  JsonObject,
  ReadFieldRequest,
  fieldName,
  readBoolean,
  readChoice,
  readConfigFile,
  readCostCenter,
  readLogRetention,
  readObject,
  readRemovalPolicy,
  readString,
} from "./app-config";
import { enumMember, enumValues, stringPattern } from "./contracts";

/** One destination's deployer configuration, parsed from one JSON file. */
export interface DestinationAppConfig {
  /** The destination's name; its stack is `CustomConnectorDestinationFor<name in PascalCase>`. */
  readonly name: string;
  /** One of the API's `DestinationType` values. */
  readonly type: string;
  readonly accessControlEnabled: boolean;
  /** The `BEDROCK` knowledge base's custom metadata fields; unset is none. */
  readonly metadataFields: readonly MetadataFieldProps[];
  /** `RETAIN` keeps the destination, its role, and its boundary when the stack is deleted. */
  readonly removalPolicy: RemovalPolicy;
  /** The retention of the destination provider's log groups. */
  readonly logRetention: RetentionDays;
  readonly costCenter: string;
  /** The framework stack's outputs, for a destination in another account; unset reads the exports. */
  readonly framework?: FrameworkValuesLiterals;
}

export interface LoadDestinationAppConfigRequest {
  /** The configuration file path, relative to the working directory or absolute. */
  readonly path: string;
}

const CONFIG_KEYS = [
  "name",
  "type",
  "accessControlEnabled",
  "metadataFields",
  "removalPolicy",
  "logRetentionDays",
  "costCenter",
  "framework",
] as const;
const METADATA_FIELD_KEYS = ["name", "type", "searchability"] as const;
const FRAMEWORK_KEYS = [
  "endpoint",
  "serviceAccount",
  "batchExecutionRoleArn",
  "destinationWorkflowArn",
] as const;

/** The API's `ResourceName` rule. */
const readName = (request: ReadFieldRequest): string => {
  const value = readString(request);
  if (!stringPattern("ResourceName").test(value))
    throw new ConfigurationError(
      `"${fieldName(request)}" must be 1 to 64 letters, digits, '.', '_', or '-'.`,
    );
  return value;
};

/** One of the API's `DestinationType` values. */
const readType = (request: ReadFieldRequest): string =>
  readChoice({ ...request, choices: enumValues("DestinationType") });

const readFramework = (object: JsonObject): FrameworkValuesLiterals | undefined => {
  if (object.framework === undefined) return undefined;
  const parent = "framework";
  const framework = readObject({ value: object.framework, field: parent, keys: FRAMEWORK_KEYS });
  const read = (key: (typeof FRAMEWORK_KEYS)[number]): string =>
    readString({ object: framework, parent, key });
  return {
    endpoint: read("endpoint"),
    serviceAccount: read("serviceAccount"),
    batchExecutionRoleArn: read("batchExecutionRoleArn"),
    destinationWorkflowArn: read("destinationWorkflowArn"),
  };
};

/** Each entry's members; the `Destination` construct checks the names, the count, and the type. */
const readMetadataFields = (object: JsonObject): readonly MetadataFieldProps[] => {
  const key = "metadataFields";
  if (object[key] === undefined) return [];
  const value = object[key];
  if (!Array.isArray(value))
    throw new ConfigurationError(`"${key}" must be a list of metadata field objects.`);
  const entries: readonly unknown[] = value;
  return entries.map((entry, index) => {
    const parent = `${key}[${index}]`;
    const field = readObject({ value: entry, field: parent, keys: METADATA_FIELD_KEYS });
    return {
      name: readString({ object: field, parent, key: "name" }),
      type: readChoice({
        object: field,
        parent,
        key: "type",
        choices: enumValues("MetadataFieldType"),
      }),
      searchability: readChoice({
        object: field,
        parent,
        key: "searchability",
        choices: enumValues("MetadataFieldSearchability"),
      }),
    };
  });
};

/** Parse one destination's configuration document. Throws {@link ConfigurationError}. */
export const parseDestinationAppConfig = (document: unknown): DestinationAppConfig => {
  const parent = "";
  const object = readObject({ value: document, field: parent, keys: CONFIG_KEYS });
  const type = readType({ object, parent, key: "type" });
  const accessControlEnabled = readBoolean({ object, parent, key: "accessControlEnabled" });
  if (accessControlEnabled && type !== enumMember({ schema: "DestinationType", member: "BEDROCK" }))
    throw new ConfigurationError(
      '"accessControlEnabled" may be true only for a BEDROCK destination.',
    );
  const framework = readFramework(object);
  return {
    name: readName({ object, parent, key: "name" }),
    type,
    accessControlEnabled,
    metadataFields: readMetadataFields(object),
    removalPolicy: readRemovalPolicy({ object, parent, key: "removalPolicy" }),
    logRetention: readLogRetention({ object, parent, key: "logRetentionDays" }),
    costCenter: readCostCenter({ object, parent, key: "costCenter" }),
    ...(framework === undefined ? {} : { framework }),
  };
};

/** Read and parse a destination configuration file. The error names the file and the field. */
export const loadDestinationAppConfig = ({
  path,
}: LoadDestinationAppConfigRequest): DestinationAppConfig =>
  readConfigFile({ path, parse: parseDestinationAppConfig });
