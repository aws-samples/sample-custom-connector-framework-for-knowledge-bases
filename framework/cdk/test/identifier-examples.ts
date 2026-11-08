import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { isJsonObject } from "../lib/common/app-config";

/** The examples the Python identifier tests read too, so both languages pin one key layout. */
const EXAMPLES_FILE = resolve(
  __dirname,
  "../../api/packages/ccf-api-models/src/ccf_api_models/contracts/identifier_examples.json",
);

export interface ConnectorKeyExample {
  readonly connectorId: string;
  /** What comes before the connector ID in its key prefix, such as `connector_id=`. */
  readonly keyStart: string;
  /** What comes after it, such as `/`. */
  readonly keyEnd: string;
}

/** The example custom connector ID and its key prefix, split around the ID. */
export const connectorKeyExample = (): ConnectorKeyExample => {
  const examples: unknown = JSON.parse(readFileSync(EXAMPLES_FILE, "utf8"));
  if (!isJsonObject(examples)) throw new Error(`${EXAMPLES_FILE} isn't a JSON object.`);
  const { connectorId, connectorKeyPrefix } = examples;
  if (typeof connectorId !== "string" || typeof connectorKeyPrefix !== "string")
    throw new Error(`${EXAMPLES_FILE} has no connectorId and connectorKeyPrefix strings.`);
  const [keyStart, keyEnd] = connectorKeyPrefix.split(connectorId);
  return { connectorId, keyStart, keyEnd };
};
