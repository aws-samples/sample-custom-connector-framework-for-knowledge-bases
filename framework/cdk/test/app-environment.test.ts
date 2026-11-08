import { App } from "aws-cdk-lib";
import { readAppEnvironment, readContextPath } from "../lib/common/app-environment";

const ACCOUNT = "111122223333";
const REGION = "us-west-2";

describe("readAppEnvironment", () => {
  it("returns the account and Region when both are set", () => {
    expect(
      readAppEnvironment({
        environment: { CDK_DEFAULT_ACCOUNT: ACCOUNT, CDK_DEFAULT_REGION: REGION },
      }),
    ).toEqual({ account: ACCOUNT, region: REGION });
  });

  it("fails naming CDK_DEFAULT_ACCOUNT when the account is unset", () => {
    expect(() => readAppEnvironment({ environment: { CDK_DEFAULT_REGION: REGION } })).toThrow(
      /^CDK_DEFAULT_ACCOUNT is not set/,
    );
  });

  it("fails naming CDK_DEFAULT_REGION when the Region is unset, instead of defaulting to us-east-1", () => {
    expect(() => readAppEnvironment({ environment: { CDK_DEFAULT_ACCOUNT: ACCOUNT } })).toThrow(
      /^CDK_DEFAULT_REGION is not set/,
    );
  });

  it("fails naming CDK_DEFAULT_REGION when the Region is empty", () => {
    expect(() =>
      readAppEnvironment({ environment: { CDK_DEFAULT_ACCOUNT: ACCOUNT, CDK_DEFAULT_REGION: "" } }),
    ).toThrow(/^CDK_DEFAULT_REGION is not set/);
  });
});

describe("readContextPath", () => {
  const appWith = (context: Record<string, unknown>): App => new App({ context });

  it("returns undefined when the key is unset", () => {
    expect(readContextPath({ app: appWith({}), key: "config" })).toBeUndefined();
  });

  it("returns a path", () => {
    expect(
      readContextPath({ app: appWith({ config: "config/example.json" }), key: "config" }),
    ).toBe("config/example.json");
  });

  it("fails naming the key for an empty string", () => {
    expect(() => readContextPath({ app: appWith({ config: "" }), key: "config" })).toThrow(
      'The "config" context value must be a configuration file path.',
    );
  });

  it("fails naming the key for a number", () => {
    expect(() =>
      readContextPath({ app: appWith({ destinationConfig: 42 }), key: "destinationConfig" }),
    ).toThrow('The "destinationConfig" context value must be a configuration file path.');
  });
});
