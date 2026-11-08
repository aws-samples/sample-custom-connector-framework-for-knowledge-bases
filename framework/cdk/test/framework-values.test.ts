import { Token } from "aws-cdk-lib";
import { FrameworkValues, frameworkLiteralsProblem } from "../lib/constructs/framework-values";
import { FRAMEWORK_LITERALS, SERVICE_ACCOUNT } from "./construct-fixtures";

describe("FrameworkValues.fromConfig with literals", () => {
  it("reads the REST API ID and Region from the endpoint, outside the service account", () => {
    const framework = FrameworkValues.fromConfig(FRAMEWORK_LITERALS);

    expect(framework.frameworkApi()).toEqual({
      endpoint: FRAMEWORK_LITERALS.endpoint,
      restApiId: "abcdef1234",
      region: "us-east-1",
      account: SERVICE_ACCOUNT,
    });
    expect(framework.inServiceAccount).toBe(false);
  });

  it("fails synthesis on literals that can't name a framework", () => {
    expect(() =>
      FrameworkValues.fromConfig({ ...FRAMEWORK_LITERALS, serviceAccount: "4444" }),
    ).toThrow('serviceAccount "4444" must be a 12-digit account ID.');
  });
});

describe("FrameworkValues.fromConfig without literals", () => {
  const framework = FrameworkValues.fromConfig();

  it("is in the service account", () => {
    expect(framework.inServiceAccount).toBe(true);
  });

  it.each([
    ["endpoint", framework.endpoint],
    ["serviceAccount", framework.serviceAccount],
    ["batchExecutionRoleArn", framework.batchExecutionRoleArn],
    ["destinationWorkflowArn", framework.destinationWorkflowArn],
    ["restApiId", framework.frameworkApi().restApiId],
  ])("reads %s from the framework's exports, as a token", (_name, value) => {
    expect(Token.isUnresolved(value)).toBe(true);
  });
});

describe("frameworkLiteralsProblem", () => {
  it.each([
    "https://abcdef1234.execute-api.us-east-1.amazonaws.com/ccf",
    "https://abcdef1234.execute-api.cn-north-1.amazonaws.com.cn/ccf",
    "https://abcdef1234.execute-api.eusc-de-east-1.amazonaws.eu/ccf",
  ])("accepts the endpoint %s", (endpoint) => {
    expect(frameworkLiteralsProblem({ ...FRAMEWORK_LITERALS, endpoint })).toBeUndefined();
  });

  it.each([
    "https://api.example.com/ccf",
    "http://abcdef1234.execute-api.us-east-1.amazonaws.com/ccf",
    "https://abcdef123.execute-api.us-east-1.amazonaws.com/ccf",
  ])("rejects the endpoint %s, naming endpoint", (endpoint) => {
    expect(frameworkLiteralsProblem({ ...FRAMEWORK_LITERALS, endpoint })).toBe(
      `endpoint "${endpoint}" must have the form https://<rest api id>.execute-api.<region>.<suffix>/<stage>.`,
    );
  });

  it("rejects a service account that isn't 12 digits, naming serviceAccount", () => {
    expect(frameworkLiteralsProblem({ ...FRAMEWORK_LITERALS, serviceAccount: "4444" })).toBe(
      'serviceAccount "4444" must be a 12-digit account ID.',
    );
  });

  it("rejects a Batch execution role in another account, naming batchExecutionRoleArn", () => {
    const batchExecutionRoleArn = "arn:aws:iam::111122223333:role/ccf/CcfBatchExecutionRole";

    expect(frameworkLiteralsProblem({ ...FRAMEWORK_LITERALS, batchExecutionRoleArn })).toBe(
      `batchExecutionRoleArn "${batchExecutionRoleArn}" must be an IAM role ARN in the service account.`,
    );
  });

  it("rejects a destination workflow that isn't a state machine, naming destinationWorkflowArn", () => {
    const destinationWorkflowArn = `arn:aws:lambda:us-east-1:${SERVICE_ACCOUNT}:function:CcfDestinationWorkflow`;

    expect(frameworkLiteralsProblem({ ...FRAMEWORK_LITERALS, destinationWorkflowArn })).toBe(
      `destinationWorkflowArn "${destinationWorkflowArn}" must be a state machine ARN in the service account.`,
    );
  });
});
