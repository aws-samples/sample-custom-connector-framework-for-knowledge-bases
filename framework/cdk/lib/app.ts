#!/usr/bin/env node
import { App } from "aws-cdk-lib";
import { DEFAULT_CONFIG_PATH, loadFrameworkAppConfig } from "./common/app-config";
import { readAppEnvironment, readContextPath } from "./common/app-environment";
import { loadDestinationAppConfig } from "./common/destination-config";
import { buildDestinationApp, buildFrameworkApp } from "./framework-app";

const app = new App();
const { account, region } = readAppEnvironment({ environment: process.env });
// With `--context destinationConfig=<file>`, the app holds only that destination's stack.
const destinationConfigPath = readContextPath({ app, key: "destinationConfig" });

if (destinationConfigPath === undefined)
  buildFrameworkApp({
    app,
    account,
    region,
    config: loadFrameworkAppConfig({
      path: readContextPath({ app, key: "config" }) ?? DEFAULT_CONFIG_PATH,
      serviceAccount: account,
    }),
  });
else
  buildDestinationApp({
    app,
    account,
    region,
    config: loadDestinationAppConfig({ path: destinationConfigPath }),
  });
app.synth();
