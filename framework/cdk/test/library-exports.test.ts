import * as library from "../lib/index";

describe("the CDK library's public surface", () => {
  it("exports exactly the constructs, patterns, and helpers destination and connector projects use", () => {
    expect(Object.keys(library).sort()).toEqual([
      "ConfigurationError",
      "ConnectorImage",
      "CustomConnector",
      "CustomConnectorDestination",
      "CustomConnectorDestinationStack",
      "CustomConnectorProject",
      "Destination",
      "DestinationOutput",
      "DestinationRole",
      "DestinationRoleName",
      "FRAMEWORK_ENDPOINT_PATTERN",
      "FrameworkValues",
      "computeProblem",
      "destinationBucketPolicy",
      "destinationStackName",
      "fieldName",
      "frameworkLiteralsProblem",
      "isJsonObject",
      "readAppEnvironment",
      "readBoolean",
      "readChoice",
      "readConfigFile",
      "readContextPath",
      "readInteger",
      "readLogRetention",
      "readNumber",
      "readObject",
      "readRemovalPolicy",
      "readString",
      "readStringList",
      "repositoryNameProblem",
      "requireValue",
      "sourceAccessServicesProblem",
      "stringPattern",
    ]);
  });

  it("checks a destination ID with the model's pattern", () => {
    expect(library.stringPattern("DestinationId").test("dst-7c41e9a2b5d0")).toBe(true);
    expect(library.stringPattern("DestinationId").test("dst-XYZ")).toBe(false);
  });
});
