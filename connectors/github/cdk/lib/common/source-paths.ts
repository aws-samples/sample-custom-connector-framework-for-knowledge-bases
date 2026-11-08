import * as path from "node:path";

/**
 * The repository root. The connector image builds from it, because the image installs
 * the CCF toolkit and API models from framework/ next to this connector's backend.
 */
export const REPOSITORY_ROOT = path.join(__dirname, "..", "..", "..", "..", "..");

/** This connector's directory, which holds backend/ next to this CDK app. */
export const CONNECTOR_ROOT = path.join(__dirname, "..", "..", "..");

/** A path inside this connector, relative to the repository root, as Dockerfile paths must be. */
export const connectorPathFromRepositoryRoot = (pathInConnector: string): string =>
  path.relative(REPOSITORY_ROOT, path.join(CONNECTOR_ROOT, pathInConnector));

/** The backend's `GitHubConnectorConfiguration` as JSON Schema, which `make build` regenerates. */
export const CONNECTOR_CONFIGURATION_SCHEMA = path.join(
  CONNECTOR_ROOT,
  "backend",
  "generated",
  "connector-configuration.schema.json",
);
