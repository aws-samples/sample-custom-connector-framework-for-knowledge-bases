import { App } from "aws-cdk-lib";

export interface ReadAppEnvironmentRequest {
  /** The process environment, `process.env` in an app entry point. */
  readonly environment: Readonly<Record<string, string | undefined>>;
}

/** The account and Region every stack of an app deploys to. */
export interface AppEnvironment {
  readonly account: string;
  readonly region: string;
}

export interface ReadContextPathRequest {
  readonly app: App;
  readonly key: string;
}

/** `CDK_DEFAULT_ACCOUNT` and `CDK_DEFAULT_REGION`, which the CDK CLI sets; fails naming the one that's unset. */
export const readAppEnvironment = ({ environment }: ReadAppEnvironmentRequest): AppEnvironment => {
  const account = environment.CDK_DEFAULT_ACCOUNT;
  if (account === undefined || account.length === 0)
    throw new Error(
      "CDK_DEFAULT_ACCOUNT is not set. The stacks' policies name the account they deploy to.",
    );
  const region = environment.CDK_DEFAULT_REGION;
  if (region === undefined || region.length === 0)
    throw new Error(
      "CDK_DEFAULT_REGION is not set. Set it, or AWS_REGION for the CDK CLI, to the Region to deploy to.",
    );
  return { account, region };
};

/** A `--context <key>=<path>` value; unset returns undefined, and anything but a non-empty string fails. */
export const readContextPath = ({ app, key }: ReadContextPathRequest): string | undefined => {
  const value: unknown = app.node.tryGetContext(key);
  if (value === undefined) return undefined;
  if (typeof value !== "string" || value.length === 0)
    throw new Error(`The "${key}" context value must be a configuration file path.`);
  return value;
};
