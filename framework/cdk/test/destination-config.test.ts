import { resolve } from "node:path";
import { RemovalPolicy } from "aws-cdk-lib";
import { RetentionDays } from "aws-cdk-lib/aws-logs";
import { ConfigurationError } from "../lib/common/app-config";
import {
  loadDestinationAppConfig,
  parseDestinationAppConfig,
} from "../lib/common/destination-config";
import { FRAMEWORK_LITERALS } from "./construct-fixtures";

const MINIMAL = {
  name: "GitHub",
  type: "BEDROCK",
  accessControlEnabled: false,
  removalPolicy: "RETAIN",
  logRetentionDays: 30,
  costCenter: "unassigned",
};

describe("parseDestinationAppConfig", () => {
  it("reads a document with every key", () => {
    expect(parseDestinationAppConfig(MINIMAL)).toEqual({
      name: "GitHub",
      type: "BEDROCK",
      accessControlEnabled: false,
      metadataFields: [],
      removalPolicy: RemovalPolicy.RETAIN,
      logRetention: RetentionDays.ONE_MONTH,
      costCenter: "unassigned",
    });
  });

  it("reads the metadata fields", () => {
    const owner = { name: "owner", type: "STRING", searchability: "ENABLED" };

    expect(
      parseDestinationAppConfig({ ...MINIMAL, metadataFields: [owner] }).metadataFields,
    ).toEqual([owner]);
  });

  it("reads the framework values for a destination in another account", () => {
    expect(
      parseDestinationAppConfig({ ...MINIMAL, framework: FRAMEWORK_LITERALS }).framework,
    ).toEqual(FRAMEWORK_LITERALS);
  });

  it.each([
    "name",
    "type",
    "accessControlEnabled",
    "removalPolicy",
    "logRetentionDays",
    "costCenter",
  ])("rejects a document without %s, naming the key", (key) => {
    const document = Object.fromEntries(Object.entries(MINIMAL).filter(([name]) => name !== key));
    expect(() => parseDestinationAppConfig(document)).toThrow(`"${key}" is required and missing.`);
  });

  it.each([
    [{ ...MINIMAL, name: "has space" }, /"name" must be 1 to 64/],
    [{ ...MINIMAL, type: "KENDRA" }, /"type" must be one of BEDROCK, QUICK, S3/],
    [{ ...MINIMAL, type: "S3", accessControlEnabled: true }, /"accessControlEnabled"/],
    [{ ...MINIMAL, accessControlEnabled: "yes" }, /"accessControlEnabled" must be true or false/],
    [{ ...MINIMAL, removalPolicy: "SNAPSHOT" }, /"removalPolicy" must be one of/],
    [{ ...MINIMAL, logRetentionDays: 2 }, /"logRetentionDays"/],
    [{ ...MINIMAL, destinationId: "dst-7c41e9a2b5d0" }, /"destinationId" is not a known/],
    [
      { ...MINIMAL, framework: { endpoint: FRAMEWORK_LITERALS.endpoint } },
      /"framework.serviceAccount" is required/,
    ],
    [
      { ...MINIMAL, framework: { ...FRAMEWORK_LITERALS, account: "1" } },
      /"framework.account" is not a known/,
    ],
    [{ ...MINIMAL, metadataFields: "owner" }, /"metadataFields" must be a list/],
    [
      { ...MINIMAL, metadataFields: [{ name: "owner", type: "NUMBER", searchability: "ENABLED" }] },
      /"metadataFields\[0\].type" must be one of STRING, STRING_LIST/,
    ],
    [
      { ...MINIMAL, metadataFields: [{ name: "owner", type: "STRING" }] },
      /"metadataFields\[0\].searchability" is required/,
    ],
  ])("rejects %j, naming the field", (document, message) => {
    expect(() => parseDestinationAppConfig(document)).toThrow(message);
  });
});

describe("loadDestinationAppConfig", () => {
  it("reads the checked-in GitHub destination", () => {
    expect(
      loadDestinationAppConfig({
        path: resolve(__dirname, "..", "config", "destination-github.json"),
      }),
    ).toMatchObject({
      name: "GitHub",
      type: "BEDROCK",
      removalPolicy: RemovalPolicy.RETAIN,
      metadataFields: ["owner", "repository", "path", "title"].map((name) => ({
        name,
        type: "STRING",
        searchability: "ENABLED",
      })),
    });
  });

  it("names the file it can't read", () => {
    expect(() => loadDestinationAppConfig({ path: "/nonexistent/destination.json" })).toThrow(
      ConfigurationError,
    );
  });
});
