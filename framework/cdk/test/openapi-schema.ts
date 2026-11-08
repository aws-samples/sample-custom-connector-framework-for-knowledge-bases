import { isJsonObject } from "../lib/common/app-config";
import { OpenApiSchema, openApi } from "../lib/common/contracts";

/** A value checked against one schema of the packaged `openapi.json`, at a JSON path for the messages. */
export interface SchemaCheckRequest {
  readonly schema: OpenApiSchema;
  readonly value: unknown;
  readonly at: string;
}

const REFERENCE_PREFIX = "#/components/schemas/";

/** One component schema of the packaged `openapi.json`, such as `CreateCustomConnectorRequestContent`. */
export const componentSchema = (name: string): OpenApiSchema => {
  const { schemas } = openApi().components;
  if (!Object.hasOwn(schemas, name)) throw new Error(`openapi.json has no schema ${name}.`);
  return schemas[name];
};

const referenced = (schema: OpenApiSchema): OpenApiSchema => {
  const reference = schema.$ref;
  return typeof reference === "string"
    ? componentSchema(reference.replace(REFERENCE_PREFIX, ""))
    : schema;
};

const isSchema = (value: unknown): value is OpenApiSchema => isJsonObject(value);
const isStringList = (value: unknown): value is readonly string[] =>
  Array.isArray(value) && value.every((item) => typeof item === "string");

const objectProblems = ({ schema, value, at }: SchemaCheckRequest): string[] => {
  if (!isJsonObject(value)) return [`${at} isn't an object.`];
  const properties = schema.properties ?? {};
  const required = isStringList(schema.required) ? schema.required : [];
  const missing = required
    .filter((name) => !Object.hasOwn(value, name))
    .map((name) => `${at}.${name} is missing.`);
  const unknown =
    schema.additionalProperties === false
      ? Object.keys(value)
          .filter((name) => !Object.hasOwn(properties, name))
          .map((name) => `${at}.${name} isn't in the schema.`)
      : [];
  const nested = Object.entries(value)
    .filter(([name]) => Object.hasOwn(properties, name))
    .flatMap(([name, member]) =>
      schemaProblems({ schema: properties[name], value: member, at: `${at}.${name}` }),
    );
  return [...missing, ...unknown, ...nested];
};

const arrayProblems = ({ schema, value, at }: SchemaCheckRequest): string[] => {
  if (!Array.isArray(value)) return [`${at} isn't an array.`];
  const { minItems, maxItems, items } = schema;
  const size =
    (typeof minItems === "number" && value.length < minItems) ||
    (typeof maxItems === "number" && value.length > maxItems)
      ? [`${at} has ${value.length} items.`]
      : [];
  const nested = isSchema(items)
    ? value.flatMap((item, index) =>
        schemaProblems({ schema: items, value: item, at: `${at}[${index}]` }),
      )
    : [];
  return [...size, ...nested];
};

const stringProblems = ({ schema, value, at }: SchemaCheckRequest): string[] => {
  if (typeof value !== "string") return [`${at} isn't a string.`];
  const { enum: values, pattern, minLength, maxLength } = schema;
  return [
    ...(values !== undefined && !values.includes(value)
      ? [`${at} "${value}" isn't one of ${values.join(", ")}.`]
      : []),
    ...(pattern !== undefined && !new RegExp(pattern).test(value)
      ? [`${at} "${value}" doesn't match ${pattern}.`]
      : []),
    ...((minLength !== undefined && value.length < minLength) ||
    (maxLength !== undefined && value.length > maxLength)
      ? [`${at} has ${value.length} characters.`]
      : []),
  ];
};

const numberProblems = ({ schema, value, at }: SchemaCheckRequest): string[] => {
  if (typeof value !== "number") return [`${at} isn't a number.`];
  const { type, minimum, maximum } = schema;
  return [
    ...(type === "integer" && !Number.isInteger(value) ? [`${at} ${value} isn't an integer.`] : []),
    ...((minimum !== undefined && value < minimum) || (maximum !== undefined && value > maximum)
      ? [`${at} ${value} is out of range.`]
      : []),
  ];
};

/** Every way a value breaks a schema: missing, unknown, or mistyped members, and patterns, lengths, and ranges. */
export const schemaProblems = (request: SchemaCheckRequest): string[] => {
  const schema = referenced(request.schema);
  const checked = { ...request, schema };
  switch (schema.type) {
    case "object":
      return objectProblems(checked);
    case "array":
      return arrayProblems(checked);
    case "string":
      return stringProblems(checked);
    case "number":
    case "integer":
      return numberProblems(checked);
    default:
      return [];
  }
};
