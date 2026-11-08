import { Duration, RemovalPolicy, Stack } from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import { RetentionDays } from "aws-cdk-lib/aws-logs";
import { ResourceName } from "../lib/common/config";
import { createBackendBundle } from "../lib/construct-groups/backend-bundle";
import { BackendFunction } from "../lib/constructs/backend-function";
import { IMAGE_DIRECTORY, SERVICE_ACCOUNT, createApp, onlyResourceId } from "./construct-fixtures";

const stack = new Stack(createApp(), "Functions", { env: { account: SERVICE_ACCOUNT } });
new BackendFunction(stack, "Backend", {
  name: ResourceName.API_LAMBDA,
  handler: "api_handler.handler",
  bundle: createBackendBundle({ sourcePath: IMAGE_DIRECTORY }),
  environment: { TABLES__JOBS: "CcfJobs" },
  memorySize: 512,
  timeout: Duration.seconds(17),
  logRetention: RetentionDays.ONE_WEEK,
  logRemovalPolicy: RemovalPolicy.DESTROY,
});
const template = Template.fromStack(stack);

describe("BackendFunction", () => {
  it("runs the handler on arm64 Python 3.14 under its fixed name", () => {
    template.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: "CcfApiLambda",
      Handler: "api_handler.handler",
      Runtime: "python3.14",
      Architectures: ["arm64"],
    });
  });

  it("is traced with X-Ray", () => {
    template.hasResourceProperties("AWS::Lambda::Function", { TracingConfig: { Mode: "Active" } });
  });

  it("takes its memory and timeout from its props", () => {
    template.hasResourceProperties("AWS::Lambda::Function", { MemorySize: 512, Timeout: 17 });
  });

  it("sets exactly the environment it is given", () => {
    template.hasResourceProperties("AWS::Lambda::Function", {
      Environment: { Variables: { TABLES__JOBS: "CcfJobs" } },
    });
  });

  it("logs to its own log group with the given retention and removal policy", () => {
    const logGroupId = onlyResourceId(template, "AWS::Logs::LogGroup");
    template.hasResource("AWS::Logs::LogGroup", {
      Properties: { RetentionInDays: 7 },
      DeletionPolicy: "Delete",
    });
    template.hasResourceProperties("AWS::Lambda::Function", {
      LoggingConfig: { LogGroup: { Ref: logGroupId } },
    });
  });

  it("creates its own role when none is given", () => {
    template.hasResourceProperties("AWS::Lambda::Function", {
      Role: { "Fn::GetAtt": [Match.stringLikeRegexp("ServiceRole"), "Arn"] },
    });
  });
});
