import { Aws, RemovalPolicy, Stack } from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { RetentionDays } from "aws-cdk-lib/aws-logs";
import { apiPathPrefix, openApi } from "../lib/common/contracts";
import { API_STAGE_NAME, ApiDefinition } from "../lib/constructs/api-definition";
import {
  JsonObject,
  SERVICE_ACCOUNT,
  TENANT_ACCOUNT,
  createApp,
  propertiesOf,
  resourcesOf,
} from "./construct-fixtures";

const FUNCTION_ARN = `arn:aws:lambda:us-east-1:${SERVICE_ACCOUNT}:function:CcfApiLambda`;

const stack = new Stack(createApp(), "Api", {
  env: { account: SERVICE_ACCOUNT, region: "us-east-1" },
});
new ApiDefinition(stack, "Definition", {
  functionArn: FUNCTION_ARN,
  throttling: { rateLimit: 20, burstLimit: 40 },
  invokerAccountIds: [SERVICE_ACCOUNT, TENANT_ACCOUNT, TENANT_ACCOUNT],
  logRetention: RetentionDays.ONE_WEEK,
  logRemovalPolicy: RemovalPolicy.DESTROY,
});
const template = Template.fromStack(stack);
const [restApi] = resourcesOf(template, "AWS::ApiGateway::RestApi").map(propertiesOf);
const body = stack.resolve(restApi.Body) as {
  readonly paths: Record<string, Record<string, JsonObject>>;
  readonly [extension: string]: unknown;
};
const operations = Object.values(body.paths).flatMap((pathItem) => Object.values(pathItem));

describe("ApiDefinition", () => {
  it("integrates every operation of the generated document with the Lambda by proxy", () => {
    const generated = Object.values(openApi().paths).flatMap((pathItem) => Object.values(pathItem));
    expect(operations).toHaveLength(generated.length);
    for (const operation of operations)
      expect(operation["x-amazon-apigateway-integration"]).toEqual({
        type: "aws_proxy",
        httpMethod: "POST",
        uri: stack.resolve(
          `arn:${Aws.PARTITION}:apigateway:${Aws.REGION}:lambda:path/2015-03-31/functions/${FUNCTION_ARN}/invocations`,
        ) as unknown,
      });
  });

  it("keeps the generated gateway responses and their templates", () => {
    const responses = body["x-amazon-apigateway-gateway-responses"] as JsonObject;
    expect(Object.keys(responses).sort()).toEqual(["BAD_REQUEST_BODY", "BAD_REQUEST_PARAMETERS"]);
    expect(responses).toEqual(
      (openApi() as unknown as JsonObject)["x-amazon-apigateway-gateway-responses"],
    );
  });

  it("lets only the given accounts invoke routes under the API's path prefix", () => {
    const [statement] = (restApi.Policy as { Statement: JsonObject[] }).Statement;
    expect(statement.Effect).toBe("Allow");
    expect(statement.Action).toBe("execute-api:Invoke");
    expect(JSON.stringify(stack.resolve(statement.Resource))).toContain(`*/*/*${apiPathPrefix()}*`);
    const principals = JSON.stringify((statement.Principal as JsonObject).AWS);
    expect(principals).toContain(`:iam::${SERVICE_ACCOUNT}:root`);
    expect(principals.split(`:iam::${TENANT_ACCOUNT}:root`)).toHaveLength(2);
  });

  it("deploys one throttled, traced stage with access logs", () => {
    template.hasResourceProperties("AWS::ApiGateway::Stage", {
      StageName: API_STAGE_NAME,
      TracingEnabled: true,
      MethodSettings: [
        {
          DataTraceEnabled: false,
          HttpMethod: "*",
          LoggingLevel: "INFO",
          MetricsEnabled: true,
          ResourcePath: "/*",
          ThrottlingRateLimit: 20,
          ThrottlingBurstLimit: 40,
        },
      ],
    });
    template.hasResourceProperties("AWS::Logs::LogGroup", { RetentionInDays: 7 });
  });
});
