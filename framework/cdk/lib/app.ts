#!/usr/bin/env node
import { App } from "aws-cdk-lib";
import { DEFAULT_CONFIG_PATH, loadFrameworkAppConfig } from "./common/app-config";
import { readAppEnvironment, readContextPath } from "./common/app-environment";
import { buildFrameworkApp } from "./framework-app";

const app = new App();
const { account, region } = readAppEnvironment({ environment: process.env });

buildFrameworkApp({
  app,
  account,
  region,
  config: loadFrameworkAppConfig({
    path: readContextPath({ app, key: "config" }) ?? DEFAULT_CONFIG_PATH,
    serviceAccount: account,
  }),
});
app.synth();
