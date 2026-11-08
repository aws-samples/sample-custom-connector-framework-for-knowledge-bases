import { Aws, RemovalPolicy } from "aws-cdk-lib";
import {
  ApiDefinition as RestApiDefinition,
  LogGroupLogDestination,
  MethodLoggingLevel,
  SpecRestApi,
} from "aws-cdk-lib/aws-apigateway";
import { AccountPrincipal, Effect, PolicyDocument, PolicyStatement } from "aws-cdk-lib/aws-iam";
import { LogGroup, RetentionDays } from "aws-cdk-lib/aws-logs";
import { Construct } from "constructs";
import { ApiThrottlingConfig } from "../common/app-config";
import { ProjectName, ResourceName, createResourceName } from "../common/config";
import { apiPathPrefix, openApi } from "../common/contracts";

const INTEGRATION_EXTENSION = "x-amazon-apigateway-integration";

/** The API's one stage; every endpoint is `https://<api id>.execute-api.<region>.<suffix>/ccf/`. */
export const API_STAGE_NAME = "ccf";

export interface ApiDefinitionProps {
  /** The API Lambda's fixed-name ARN, so the body names it without a CloudFormation cycle. */
  readonly functionArn: string;
  readonly throttling: ApiThrottlingConfig;
  /** The accounts the resource policy lets call the API: the service account and each tenant account. */
  readonly invokerAccountIds: readonly string[];
  readonly logRetention: RetentionDays;
  readonly logRemovalPolicy: RemovalPolicy;
}

/** The generated OpenAPI document with every operation integrated with the API Lambda by proxy. */
const integratedBody = (functionArn: string): Record<string, unknown> => {
  const integration = {
    type: "aws_proxy",
    httpMethod: "POST",
    uri: `arn:${Aws.PARTITION}:apigateway:${Aws.REGION}:lambda:path/2015-03-31/functions/${functionArn}/invocations`,
  };
  const document = openApi();
  const paths = Object.fromEntries(
    Object.entries(document.paths).map(([path, pathItem]) => [
      path,
      Object.fromEntries(
        Object.entries(pathItem).map(([method, operation]) => [
          method,
          { ...operation, [INTEGRATION_EXTENSION]: integration },
        ]),
      ),
    ]),
  );
  return { ...document, paths };
};

/**
 * The framework's IAM-authorized REST API: the generated document (with its request validator and the
 * gateway responses that return the error envelope) integrated with the API Lambda, one stage with
 * access logs, X-Ray, and stage-wide throttling, and a resource policy that admits only the given accounts.
 */
export class ApiDefinition extends Construct {
  public readonly api: SpecRestApi;

  constructor(scope: Construct, id: string, props: ApiDefinitionProps) {
    super(scope, id);
    const { functionArn, throttling, invokerAccountIds, logRetention, logRemovalPolicy } = props;
    const accessLogGroup = new LogGroup(this, "AccessLogs", {
      retention: logRetention,
      removalPolicy: logRemovalPolicy,
    });
    this.api = new SpecRestApi(this, ResourceName.API, {
      apiDefinition: RestApiDefinition.fromInline(integratedBody(functionArn)),
      restApiName: createResourceName({
        projectName: ProjectName.CCF,
        resourceName: ResourceName.API,
      }),
      description: "Custom Connector Framework API (IAM authorized).",
      deployOptions: {
        stageName: API_STAGE_NAME,
        accessLogDestination: new LogGroupLogDestination(accessLogGroup),
        loggingLevel: MethodLoggingLevel.INFO,
        metricsEnabled: true,
        tracingEnabled: true,
        throttlingRateLimit: throttling.rateLimit,
        throttlingBurstLimit: throttling.burstLimit,
      },
      policy: new PolicyDocument({
        statements: [
          new PolicyStatement({
            sid: "ServiceAndTenantAccounts",
            effect: Effect.ALLOW,
            principals: [...new Set(invokerAccountIds)].map(
              (account) => new AccountPrincipal(account),
            ),
            actions: ["execute-api:Invoke"],
            // The API ID isn't known while the policy is written; the account principals and the
            // path keep every other account and every other API out.
            resources: [
              `arn:${Aws.PARTITION}:execute-api:${Aws.REGION}:${Aws.ACCOUNT_ID}:*/*/*${apiPathPrefix()}*`,
            ],
          }),
        ],
      }),
    });
  }
}
