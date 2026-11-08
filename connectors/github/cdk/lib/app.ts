#!/usr/bin/env node
import { App } from "aws-cdk-lib";
import { readAppEnvironment, readContextPath } from "custom-connector-framework-cdk";
import { DEFAULT_CONFIG_PATH, loadGitHubConnectorAppConfig } from "./common/app-config";
import { ConnectorConfigurationSchema } from "./common/connector-configuration-schema";
import { CONNECTOR_CONFIGURATION_SCHEMA } from "./common/source-paths";
import { buildGitHubConnectorApp } from "./github-connector-app";

const app = new App();
const { account, region } = readAppEnvironment({ environment: process.env });

buildGitHubConnectorApp({
  app,
  config: loadGitHubConnectorAppConfig({
    path: readContextPath({ app, key: "config" }) ?? DEFAULT_CONFIG_PATH,
    schema: ConnectorConfigurationSchema.fromFile(CONNECTOR_CONFIGURATION_SCHEMA),
  }),
  account,
  region,
});
app.synth();
