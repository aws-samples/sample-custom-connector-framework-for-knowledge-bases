import { App, Aspects, RemovalPolicy } from "aws-cdk-lib";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { Annotations, Match, Template } from "aws-cdk-lib/assertions";
import { AwsSolutionsChecks } from "cdk-nag";
import {
  EnvironmentName,
  Provider,
  ProjectName,
  resolveAllowedClientAccountIds,
} from "../lib/common/config";
import { NAG_SUPPRESSIONS } from "../lib/common/nag-suppressions";
import { createBackendImage } from "../lib/construct-groups/compute-image";
import { CustomConnectorFrameworkStack } from "../lib/stacks/custom-connector-framework-stack";
import { NagSuppressions } from "cdk-nag";

/**
 * `aws:cdk:bundling-stacks: []` tells CDK to skip all Docker asset bundling
 * during synth, so these tests run without a Docker daemon. The synthesized
 * template still contains the Lambda resources (pointing at an un-bundled
 * asset), which is all the assertions below inspect.
 */
const createApp = (): App => new App({ context: { "aws:cdk:bundling-stacks": [] } });
const FRAMEWORK_ACCOUNT = "123456789012";
const CLIENT_ACCOUNT = "210987654321";
const UNCONFIGURED_ACCOUNT = "210987654399";

const EXPECTED_OPERATION_IDS = [
  "BatchDeleteCustomConnectorDocuments",
  "BatchPutCustomConnectorDocuments",
  "CreateCustomConnector",
  "DeleteCustomConnector",
  "DeleteCustomConnectorCheckpoint",
  "GetCustomConnector",
  "GetCustomConnectorCheckpoint",
  "GetCustomConnectorJob",
  "ListCustomConnectorDocuments",
  "ListCustomConnectorJobs",
  "ListCustomConnectors",
  "PutCustomConnectorCheckpoint",
  "StartCustomConnectorJob",
  "StopCustomConnectorJob",
  "UpdateCustomConnector",
];
const INTEGRATION_PLACEHOLDER = "__CCF_API_LAMBDA_INTEGRATION_URI__";

type JsonObject = Record<string, unknown>;

interface ImportedOperation {
  readonly definition: JsonObject;
  readonly method: string;
}

const isJsonObject = (value: unknown): value is JsonObject =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const accountRootPrincipal = (accountId: string): JsonObject => ({
  "Fn::Join": ["", ["arn:", { Ref: "AWS::Partition" }, `:iam::${accountId}:root`]],
});

const importedOpenApi = (template: Template): JsonObject => {
  const apiResources = Object.values(template.findResources("AWS::ApiGateway::RestApi"));
  expect(apiResources).toHaveLength(1);
  const body: unknown = apiResources[0].Properties?.Body;
  if (!isJsonObject(body)) {
    throw new Error("SpecRestApi must inline its OpenAPI document in the RestApi body.");
  }
  return body;
};

const importedOperations = (openApi: JsonObject): ImportedOperation[] => {
  const paths = openApi.paths;
  if (!isJsonObject(paths)) {
    throw new Error("Imported OpenAPI must contain paths.");
  }
  return Object.values(paths).flatMap((pathValue) => {
    if (!isJsonObject(pathValue)) {
      return [];
    }
    return Object.entries(pathValue).flatMap(([method, operation]) =>
      isJsonObject(operation) && typeof operation.operationId === "string"
        ? [{ definition: operation, method }]
        : [],
    );
  });
};

const actionsForStatement = (statement: JsonObject): string[] => {
  if (typeof statement.Action === "string") {
    return [statement.Action];
  }
  if (Array.isArray(statement.Action)) {
    return statement.Action.filter((action): action is string => typeof action === "string");
  }
  return [];
};

const roleLogicalId = (template: Template, roleName: string): string => {
  const role = Object.entries(template.findResources("AWS::IAM::Role")).find(
    ([, resource]) => resource.Properties?.RoleName === roleName,
  );
  if (role === undefined) {
    throw new Error(`Expected IAM role ${roleName}.`);
  }
  return role[0];
};

const rolePolicyStatements = (template: Template, logicalRoleId: string): JsonObject[] =>
  Object.values(template.findResources("AWS::IAM::Policy"))
    .filter((policy) => {
      const roles: unknown = policy.Properties?.Roles;
      return (
        Array.isArray(roles) &&
        roles.some(
          (roleReference) => isJsonObject(roleReference) && roleReference.Ref === logicalRoleId,
        )
      );
    })
    .flatMap((policy) => (policy.Properties?.PolicyDocument?.Statement ?? []).filter(isJsonObject));

const lambdaRoleLogicalId = (template: Template, handler: string): string => {
  const lambda = Object.values(template.findResources("AWS::Lambda::Function")).find(
    (resource) => resource.Properties?.Handler === handler,
  );
  const role = lambda?.Properties?.Role;
  const roleAttribute = isJsonObject(role) ? role["Fn::GetAtt"] : undefined;
  if (!Array.isArray(roleAttribute) || typeof roleAttribute[0] !== "string") {
    throw new Error(`Expected ${handler} to reference an IAM role with Fn::GetAtt.`);
  }
  return roleAttribute[0];
};

const synth = (provider: Provider): Template => {
  const app = createApp();
  const stack = new CustomConnectorFrameworkStack(app, "TestStack", {
    projectName: ProjectName.CCF,
    environmentName: EnvironmentName.DEV,
    retainResources: false,
    account: FRAMEWORK_ACCOUNT,
    region: "us-east-1",
    allowedClientAccountIds: [CLIENT_ACCOUNT],
    provider,
    embeddingModelId: "amazon.titan-embed-text-v2:0",
    vectorDimension: 1024,
    env: { account: "123456789012", region: "us-east-1" },
  });
  return Template.fromStack(stack);
};

describe("allowed client account context", () => {
  it("trims and deduplicates strict account IDs", () => {
    expect(
      resolveAllowedClientAccountIds({
        values: ` ${CLIENT_ACCOUNT},${UNCONFIGURED_ACCOUNT},${CLIENT_ACCOUNT} `,
        frameworkAccount: FRAMEWORK_ACCOUNT,
      }),
    ).toEqual([CLIENT_ACCOUNT, UNCONFIGURED_ACCOUNT]);
  });

  it.each(["21098765432", "21098765432a", `${CLIENT_ACCOUNT},`])(
    "rejects invalid account context %s",
    (values) => {
      expect(() =>
        resolveAllowedClientAccountIds({ values, frameworkAccount: FRAMEWORK_ACCOUNT }),
      ).toThrow(/12-digit AWS account IDs/);
    },
  );

  it("rejects the framework account", () => {
    expect(() =>
      resolveAllowedClientAccountIds({
        values: FRAMEWORK_ACCOUNT,
        frameworkAccount: FRAMEWORK_ACCOUNT,
      }),
    ).toThrow(/must not include the framework account/);
  });
});

describe("CustomConnectorFrameworkStack composition", () => {
  it.each([
    [Provider.S3, 88],
    [Provider.BEDROCK, 98],
    [Provider.QUICK, 88],
  ])(
    "provider=%s bundling-disabled assertion context has exactly %i application resources",
    (provider, count) => {
      expect(Object.keys(synth(provider).toJSON().Resources)).toHaveLength(count);
    },
  );

  it("creates the three state tables with KMS encryption", () => {
    const template = synth(Provider.S3);
    template.resourceCountIs("AWS::DynamoDB::Table", 3);
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      SSESpecification: { SSEEnabled: true },
    });
  });

  it("keys document state by tenant and connector-scoped document identity", () => {
    const template = synth(Provider.S3);
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      KeySchema: [
        { AttributeName: "custom_connector_arn_prefix", KeyType: "HASH" },
        { AttributeName: "connector_document_key", KeyType: "RANGE" },
      ],
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({
          IndexName: "GSI1",
          KeySchema: [
            { AttributeName: "custom_connector_arn_prefix", KeyType: "HASH" },
            { AttributeName: "connector_id", KeyType: "RANGE" },
          ],
        }),
      ]),
    });
  });

  it("creates exactly one ingestion bucket and one access-logs bucket, both private and KMS-encrypted", () => {
    const template = synth(Provider.S3);
    template.resourceCountIs("AWS::S3::Bucket", 2);
    template.hasResourceProperties("AWS::S3::Bucket", {
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
      BucketEncryption: {
        ServerSideEncryptionConfiguration: [
          { ServerSideEncryptionByDefault: { SSEAlgorithm: "aws:kms" } },
        ],
      },
    });
  });

  it("creates the four backend Lambdas on the Python 3.14 runtime", () => {
    const template = synth(Provider.S3);
    // Count only framework handlers; CDK may add custom-resource Lambdas.
    const backendHandlers = Object.values(template.findResources("AWS::Lambda::Function")).filter(
      (fn) => /_handler\.handler$/.test((fn.Properties?.Handler as string) ?? ""),
    );
    expect(backendHandlers).toHaveLength(4);
    template.hasResourceProperties("AWS::Lambda::Function", {
      Runtime: "python3.14",
      Handler: Match.stringLikeRegexp("_handler.handler$"),
      TracingConfig: { Mode: "Active" },
    });
  });

  it("configures API configuration encryption and packages generated models", () => {
    const template = synth(Provider.S3);
    const functions = Object.values(template.findResources("AWS::Lambda::Function"));
    const apiLambda = functions.find((fn) => fn.Properties?.Handler === "api_handler.handler");
    const nonConfigurationLambdas = functions.filter((fn) =>
      ["job_orchestrator_handler.handler", "job_status_handler.handler"].includes(
        (fn.Properties?.Handler as string) ?? "",
      ),
    );
    expect(apiLambda?.Properties?.Environment?.Variables).toEqual(
      expect.objectContaining({
        CONFIGURATION_KEY_ARN: expect.anything(),
      }),
    );
    const scheduledStartLambda = functions.find(
      (fn) => fn.Properties?.Handler === "scheduled_start_handler.handler",
    );
    expect(scheduledStartLambda?.Properties?.Environment?.Variables).toEqual(
      expect.objectContaining({ CONFIGURATION_KEY_ARN: expect.anything() }),
    );
    for (const lambda of nonConfigurationLambdas) {
      expect(lambda.Properties?.Environment?.Variables).not.toHaveProperty("CONFIGURATION_KEY_ARN");
    }
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith(["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey*"]),
            Effect: "Allow",
            Resource: Match.anyValue(),
          }),
        ]),
      },
    });

    const image = createBackendImage("/repository", RemovalPolicy.DESTROY);
    const command = image.bundling.command?.join(" ") ?? "";
    expect(command).toContain("api/packages/ccf-api-models/src/ccf_api_models");
    expect(command).toContain("import api_handler, ccf_api_models");
  });

  it("imports the 15 modeled IAM operations with validation and Lambda integrations", () => {
    const template = synth(Provider.S3);
    const openApi = importedOpenApi(template);
    const operations = importedOperations(openApi);
    const operationIds = operations.map(({ definition }) => definition.operationId).sort();

    expect(operations).toHaveLength(15);
    expect(operationIds).toEqual(EXPECTED_OPERATION_IDS);
    expect(openApi.security).toEqual([{ "aws.auth.sigv4": [] }]);
    expect(openApi.components).toEqual(
      expect.objectContaining({
        securitySchemes: {
          "aws.auth.sigv4": expect.objectContaining({
            type: "apiKey",
            "x-amazon-apigateway-authtype": "awsSigv4",
          }),
        },
      }),
    );
    expect(openApi["x-amazon-apigateway-request-validators"]).toEqual({
      all: { validateRequestBody: true, validateRequestParameters: true },
    });

    for (const { definition, method } of operations) {
      expect(method).not.toBe("x-amazon-apigateway-any-method");
      const effectiveSecurity = definition.security ?? openApi.security;
      expect(effectiveSecurity).toEqual([{ "aws.auth.sigv4": [] }]);
      expect(definition["x-amazon-apigateway-request-validator"]).toBe("all");
      const integration = definition["x-amazon-apigateway-integration"];
      if (!isJsonObject(integration)) {
        throw new Error(`${String(definition.operationId)} must have an API Gateway integration.`);
      }
      expect(Object.keys(integration).sort()).toEqual(["httpMethod", "type", "uri"]);
      expect(integration.type).toBe("aws_proxy");
      expect(integration.httpMethod).toBe("POST");
      expect(JSON.stringify(integration.uri)).toContain(
        ":apigateway:us-east-1:lambda:path/2015-03-31/functions/",
      );
      expect(JSON.stringify(integration)).not.toContain(INTEGRATION_PLACEHOLDER);
    }

    const serializedOpenApi = JSON.stringify(openApi);
    expect(serializedOpenApi).not.toContain("{proxy+}");
    expect(serializedOpenApi).not.toContain("x-amazon-apigateway-any-method");
    expect(serializedOpenApi).not.toContain(INTEGRATION_PLACEHOLDER);
    template.resourceCountIs("AWS::ApiGateway::Method", 0);
    const apiPolicy: unknown = Object.values(template.findResources("AWS::ApiGateway::RestApi"))[0]
      .Properties?.Policy;
    if (!isJsonObject(apiPolicy) || !Array.isArray(apiPolicy.Statement)) {
      throw new Error("Expected an API resource policy with one statement.");
    }
    expect(apiPolicy.Statement).toEqual([
      {
        Action: "execute-api:Invoke",
        Effect: "Allow",
        Principal: {
          AWS: [accountRootPrincipal(FRAMEWORK_ACCOUNT), accountRootPrincipal(CLIENT_ACCOUNT)],
        },
        Resource: {
          "Fn::Join": [
            "",
            [
              "arn:",
              { Ref: "AWS::Partition" },
              `:execute-api:us-east-1:${FRAMEWORK_ACCOUNT}:*/*/*/api/v1/custom-connectors*`,
            ],
          ],
        },
      },
    ]);
    const [apiPolicyStatement] = apiPolicy.Statement;
    if (!isJsonObject(apiPolicyStatement)) {
      throw new Error("Expected a structured API resource-policy statement.");
    }
    expect(apiPolicyStatement.Principal).not.toBe("*");
    expect(apiPolicyStatement.Principal).not.toEqual({ AWS: "*" });
  });

  it("grants API Gateway source-scoped permission to invoke the API Lambda", () => {
    const template = synth(Provider.S3);
    template.hasResourceProperties("AWS::Lambda::Permission", {
      Action: "lambda:InvokeFunction",
      Principal: "apigateway.amazonaws.com",
      SourceArn: Match.anyValue(),
    });
    const apiResources = template.findResources("AWS::ApiGateway::RestApi");
    const [apiLogicalId] = Object.keys(apiResources);
    const permissions = Object.values(template.findResources("AWS::Lambda::Permission"));
    const apiGatewayPermission = permissions.find(
      (permission) => permission.Properties?.Principal === "apigateway.amazonaws.com",
    );
    const sourceArn = JSON.stringify(apiGatewayPermission?.Properties?.SourceArn);
    expect(sourceArn).toContain("execute-api");
    expect(sourceArn).toContain(apiLogicalId);
    expect(sourceArn).toContain("/*/*/");
  });

  it("keeps API access logging, INFO method logging, metrics, and tracing", () => {
    const template = synth(Provider.S3);
    template.hasResourceProperties("AWS::ApiGateway::Stage", {
      AccessLogSetting: {
        DestinationArn: Match.anyValue(),
        Format: Match.anyValue(),
      },
      MethodSettings: Match.arrayWith([
        Match.objectLike({
          HttpMethod: "*",
          LoggingLevel: "INFO",
          MetricsEnabled: true,
          ResourcePath: "/*",
        }),
      ]),
      TracingEnabled: true,
    });
    template.hasResourceProperties("AWS::Logs::LogGroup", {
      RetentionInDays: 30,
    });
  });

  it("keeps the jobs stream mapping and terminal Batch status rule", () => {
    const template = synth(Provider.S3);
    template.resourceCountIs("AWS::Lambda::EventSourceMapping", 1);
    template.hasResourceProperties("AWS::Events::Rule", {
      EventPattern: {
        source: ["aws.batch"],
        "detail-type": ["Batch Job State Change"],
        detail: {
          status: ["SUCCEEDED", "FAILED"],
          jobName: [{ prefix: "ccj-" }],
        },
      },
      Targets: Match.arrayWith([Match.objectLike({ Arn: Match.anyValue() })]),
    });
  });

  it("deploys one shared Scheduler target and no static framework schedule", () => {
    const template = synth(Provider.S3);
    template.resourceCountIs("AWS::Scheduler::ScheduleGroup", 1);
    template.resourceCountIs("AWS::Scheduler::Schedule", 0);
    const handlers = Object.values(template.findResources("AWS::Lambda::Function"));
    expect(
      handlers.filter(
        (resource) => resource.Properties?.Handler === "scheduled_start_handler.handler",
      ),
    ).toHaveLength(1);
    const apiHandler = handlers.find(
      (resource) => resource.Properties?.Handler === "api_handler.handler",
    );
    expect(apiHandler?.Properties?.Environment?.Variables).toEqual(
      expect.objectContaining({
        SCHEDULER_TARGET_ARN: expect.anything(),
        SCHEDULER_ROLE_ARN: expect.anything(),
        SCHEDULER_GROUP_NAME: "CcfSchedulerGroup",
        SCHEDULER_DLQ_ARN: expect.anything(),
        DESTINATION_MODE: "S3",
      }),
    );
  });

  it("grants exact Scheduler lifecycle and target-role pass permissions", () => {
    const template = synth(Provider.S3);
    const policies = Object.values(template.findResources("AWS::IAM::Policy"));
    const statements = policies.flatMap(
      (policy) => policy.Properties?.PolicyDocument?.Statement ?? [],
    );
    const scheduleStatement = statements.find(
      (statement: { Action?: unknown }) =>
        Array.isArray(statement.Action) && statement.Action.includes("scheduler:CreateSchedule"),
    );
    expect(scheduleStatement?.Action).toEqual([
      "scheduler:CreateSchedule",
      "scheduler:UpdateSchedule",
      "scheduler:DeleteSchedule",
      "scheduler:GetSchedule",
    ]);
    expect(JSON.stringify(scheduleStatement?.Resource)).toContain(":schedule/CcfSchedulerGroup/*");
    const schedulerPassRole = statements.find(
      (statement: { Action?: unknown; Condition?: unknown }) =>
        statement.Action === "iam:PassRole" &&
        JSON.stringify(statement.Condition).includes("scheduler.amazonaws.com"),
    );
    expect(schedulerPassRole).toEqual(
      expect.objectContaining({
        Condition: { StringEquals: { "iam:PassedToService": "scheduler.amazonaws.com" } },
        Resource: expect.anything(),
      }),
    );
    expect(JSON.stringify(schedulerPassRole?.Resource)).toContain("SchedulerTargetRole");
  });

  it("uses one exact shared Batch execution principal and PassRole condition", () => {
    const template = synth(Provider.S3);
    template.hasResourceProperties("AWS::IAM::Role", {
      RoleName: "CcfFrameworkExecutionRole",
      AssumeRolePolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({ Principal: { Service: "ecs-tasks.amazonaws.com" } }),
        ]),
      },
    });
    const statements = Object.values(template.findResources("AWS::IAM::Policy")).flatMap(
      (policy) => policy.Properties?.PolicyDocument?.Statement ?? [],
    );
    const batchPassRole = statements.find(
      (statement: { Action?: unknown; Condition?: unknown }) =>
        statement.Action === "iam:PassRole" &&
        JSON.stringify(statement.Condition).includes("ecs-tasks.amazonaws.com"),
    );
    expect(batchPassRole).toEqual(
      expect.objectContaining({
        Condition: { StringEquals: { "iam:PassedToService": "ecs-tasks.amazonaws.com" } },
        Resource: expect.anything(),
      }),
    );
    // Batch is passed exactly two identities: the shared execution role and one
    // service-managed per-connector runtime role under the service-owned path.
    expect(batchPassRole.Resource).toEqual([
      { "Fn::GetAtt": [roleLogicalId(template, "CcfFrameworkExecutionRole"), "Arn"] },
      {
        "Fn::Join": [
          "",
          [
            "arn:",
            { Ref: "AWS::Partition" },
            `:iam::${FRAMEWORK_ACCOUNT}:role/ccf-runtime/CcfConnectorRuntime-*`,
          ],
        ],
      },
    ]);

    const registerJobDefinition = statements.find(
      (statement: { Action?: unknown }) =>
        Array.isArray(statement.Action) && statement.Action.includes("batch:RegisterJobDefinition"),
    );
    expect(registerJobDefinition?.Action).toEqual([
      "batch:RegisterJobDefinition",
      "batch:TagResource",
    ]);
    expect(JSON.stringify(registerJobDefinition?.Resource)).toContain("job-definition/ccj-*");

    const submitJob = statements.find(
      (statement: { Action?: unknown }) => statement.Action === "batch:SubmitJob",
    );
    expect(submitJob?.Resource).toHaveLength(3);
    expect(JSON.stringify(submitJob?.Resource)).toContain("JobQueueArn");
    expect(JSON.stringify(submitJob?.Resource)).toContain("job-definition/ccj-*");
    expect(JSON.stringify(submitJob?.Resource)).toContain("job/*");
    expect(submitJob?.Condition).toEqual({
      StringEquals: { "aws:RequestTag/ccf:owned-by": "custom-connector-framework" },
    });

    const terminateJob = statements.find(
      (statement: { Action?: unknown }) => statement.Action === "batch:TerminateJob",
    );
    expect(JSON.stringify(terminateJob?.Resource)).toContain("job/*");
    expect(terminateJob?.Condition).toEqual({
      StringEquals: { "aws:ResourceTag/ccf:owned-by": "custom-connector-framework" },
    });
    // Replay proves ownership of a possibly submitted execution from the
    // persisted queue, job name, and definition, so listing is queue-scoped.
    const listJobs = statements.find(
      (statement: { Action?: unknown }) => statement.Action === "batch:ListJobs",
    );
    expect(listJobs?.Resource).toEqual({ "Fn::GetAtt": [expect.any(String), "JobQueueArn"] });
    expect(listJobs?.Condition).toBeUndefined();

    const serializedStatements = JSON.stringify(statements);
    expect(serializedStatements).not.toContain("batch:CancelJob");
    expect(serializedStatements).not.toContain("batch:DescribeJobs");
  });

  it("grants exact Batch job-definition cleanup permissions only to the two cleanup Lambdas", () => {
    const template = synth(Provider.S3);
    const cleanupActions = ["batch:DescribeJobDefinitions", "batch:DeregisterJobDefinition"];
    const expectedCleanupStatements = [
      {
        Action: "batch:DescribeJobDefinitions",
        Effect: "Allow",
        Resource: "*",
      },
      {
        Action: "batch:DeregisterJobDefinition",
        Effect: "Allow",
        Resource: {
          "Fn::Join": [
            "",
            [
              "arn:",
              { Ref: "AWS::Partition" },
              `:batch:us-east-1:${FRAMEWORK_ACCOUNT}:job-definition/ccj-*:*`,
            ],
          ],
        },
      },
    ];

    for (const handler of ["job_orchestrator_handler.handler", "job_status_handler.handler"]) {
      const roleId = lambdaRoleLogicalId(template, handler);
      const cleanupStatements = rolePolicyStatements(template, roleId).filter((statement) =>
        actionsForStatement(statement).some((action) => cleanupActions.includes(action)),
      );
      expect(cleanupStatements).toEqual(expectedCleanupStatements);
    }

    const allCleanupStatements = Object.values(template.findResources("AWS::IAM::Policy"))
      .flatMap((policy) =>
        (policy.Properties?.PolicyDocument?.Statement ?? []).filter(isJsonObject),
      )
      .filter((statement) =>
        actionsForStatement(statement).some((action) => cleanupActions.includes(action)),
      );
    expect(allCleanupStatements).toHaveLength(4);
  });

  it("attaches exact ECR pull and Batch log permissions only to the framework execution role", () => {
    const template = synth(Provider.S3);
    const executionRoleId = roleLogicalId(template, "CcfFrameworkExecutionRole");
    const executionStatements = rolePolicyStatements(template, executionRoleId);
    const executionEcrStatements = executionStatements.filter((statement) =>
      actionsForStatement(statement).some((action) => action.startsWith("ecr:")),
    );
    expect(executionEcrStatements).toHaveLength(2);
    expect(executionEcrStatements).toContainEqual({
      Action: "ecr:GetAuthorizationToken",
      Effect: "Allow",
      Resource: "*",
    });
    expect(executionEcrStatements).toContainEqual({
      Action: [
        "ecr:BatchCheckLayerAvailability",
        "ecr:GetDownloadUrlForLayer",
        "ecr:BatchGetImage",
      ],
      Effect: "Allow",
      Resource: {
        "Fn::Join": [
          "",
          [
            "arn:",
            { Ref: "AWS::Partition" },
            `:ecr:us-east-1:${CLIENT_ACCOUNT}:repository/ccf-client-image-assets-${CLIENT_ACCOUNT}-us-east-1`,
          ],
        ],
      },
    });

    const executionLogStatements = executionStatements.filter((statement) =>
      actionsForStatement(statement).some((action) => action.startsWith("logs:")),
    );
    expect(executionLogStatements).toEqual([
      {
        Action: ["logs:CreateLogStream", "logs:PutLogEvents"],
        Effect: "Allow",
        Resource: {
          "Fn::Join": [
            "",
            [
              "arn:",
              { Ref: "AWS::Partition" },
              ":logs:us-east-1:123456789012:log-group:/aws/batch/job:log-stream:*",
            ],
          ],
        },
      },
    ]);
    expect(actionsForStatement(executionLogStatements[0])).not.toContain("logs:CreateLogGroup");

    const allStatements = Object.values(template.findResources("AWS::IAM::Policy")).flatMap(
      (policy) => (policy.Properties?.PolicyDocument?.Statement ?? []).filter(isJsonObject),
    );
    const allEcrStatements = allStatements.filter((statement) =>
      actionsForStatement(statement).some((action) => action.startsWith("ecr:")),
    );
    expect(allEcrStatements).toEqual(executionEcrStatements);

    const orchestratorRoleId = lambdaRoleLogicalId(template, "job_orchestrator_handler.handler");
    const orchestratorActions = rolePolicyStatements(template, orchestratorRoleId).flatMap(
      actionsForStatement,
    );
    expect(orchestratorActions.filter((action) => action.startsWith("ecr:"))).toEqual([]);
  });

  it("keeps source, destination, and API access off the Batch execution role", () => {
    const template = synth(Provider.S3);
    const executionRoleId = roleLogicalId(template, "CcfFrameworkExecutionRole");
    const executionActions = rolePolicyStatements(template, executionRoleId).flatMap(
      actionsForStatement,
    );

    // Connector code never receives execution-role credentials, so that role
    // must not reach the destination bucket, the service key, the source role,
    // or the control plane.
    expect(executionActions.filter((action) => action.startsWith("s3:"))).toEqual([]);
    expect(executionActions.filter((action) => action.startsWith("kms:"))).toEqual([]);
    expect(executionActions).not.toContain("sts:AssumeRole");
    expect(executionActions).not.toContain("execute-api:Invoke");
    expect(executionActions).not.toContain("iam:PassRole");
    expect([...executionActions].sort()).toEqual(
      [
        "ecr:GetAuthorizationToken",
        "ecr:BatchCheckLayerAvailability",
        "ecr:GetDownloadUrlForLayer",
        "ecr:BatchGetImage",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
      ].sort(),
    );
  });

  it("caps every per-connector runtime role with one permissions boundary", () => {
    const template = synth(Provider.S3);
    const boundaries = Object.values(template.findResources("AWS::IAM::ManagedPolicy")).filter(
      (policy) => policy.Properties?.ManagedPolicyName === "CcfConnectorRuntimeBoundary",
    );
    expect(boundaries).toHaveLength(1);
    const statements: JsonObject[] = (
      boundaries[0].Properties?.PolicyDocument?.Statement ?? []
    ).filter(isJsonObject);
    const actions = statements.flatMap(actionsForStatement);
    expect([...actions].sort()).toEqual(
      [
        "execute-api:Invoke",
        "s3:PutObject",
        "s3:DeleteObject",
        "kms:GenerateDataKey",
        "kms:Decrypt",
        "sts:AssumeRole",
      ].sort(),
    );
    expect(actions).not.toContain("iam:PassRole");
    expect(actions).not.toContain("iam:CreateRole");
    expect(actions).not.toContain("dynamodb:GetItem");
    expect(actions).not.toContain("batch:SubmitJob");

    const serialized = JSON.stringify(statements);
    // The destination cap never leaves the documents/ prefix tree, the source
    // cap never leaves configured client accounts, and nothing is Resource:*.
    expect(serialized).toContain("/documents/*");
    expect(serialized).toContain(`:iam::${CLIENT_ACCOUNT}:role/CcfSourceRole-*`);
    expect(serialized).not.toContain('"Resource":"*"');
    expect(serialized).not.toContain(":iam::*:");

    const assumeRole = statements.find((statement) =>
      actionsForStatement(statement).includes("sts:AssumeRole"),
    );
    expect(JSON.stringify(assumeRole?.Resource)).not.toContain(FRAMEWORK_ACCOUNT);
  });

  it("lets only the control plane manage path-scoped boundary-capped runtime roles", () => {
    const template = synth(Provider.S3);
    const runtimeRoleArnPattern = `:iam::${FRAMEWORK_ACCOUNT}:role/ccf-runtime/CcfConnectorRuntime-*`;
    const apiRoleId = lambdaRoleLogicalId(template, "api_handler.handler");
    const iamStatements = rolePolicyStatements(template, apiRoleId).filter((statement) =>
      actionsForStatement(statement).some((action) => action.startsWith("iam:")),
    );
    const createRole = iamStatements.find((statement) =>
      actionsForStatement(statement).includes("iam:CreateRole"),
    );
    const boundaryLogicalId = Object.entries(
      template.findResources("AWS::IAM::ManagedPolicy"),
    ).find(
      ([, policy]) => policy.Properties?.ManagedPolicyName === "CcfConnectorRuntimeBoundary",
    )?.[0];
    expect(boundaryLogicalId).toBeDefined();
    expect(createRole?.Action).toEqual("iam:CreateRole");
    expect(JSON.stringify(createRole?.Resource)).toContain(runtimeRoleArnPattern);
    expect(createRole?.Condition).toEqual({
      StringEquals: { "iam:PermissionsBoundary": { Ref: boundaryLogicalId } },
    });

    const mutateRole = iamStatements.find((statement) =>
      actionsForStatement(statement).includes("iam:PutRolePolicy"),
    );
    expect([...actionsForStatement(mutateRole ?? {})].sort()).toEqual(
      ["iam:DeleteRole", "iam:DeleteRolePolicy", "iam:PutRolePolicy"].sort(),
    );
    expect(JSON.stringify(mutateRole?.Resource)).toContain(runtimeRoleArnPattern);

    // No other role may mint or re-scope a connector runtime role.
    const roleLifecycleActions = [
      "iam:CreateRole",
      "iam:PutRolePolicy",
      "iam:DeleteRolePolicy",
      "iam:DeleteRole",
    ];
    const isRoleLifecycle = (statement: JsonObject): boolean =>
      actionsForStatement(statement).some((action) => roleLifecycleActions.includes(action));
    const allIamStatements = Object.values(template.findResources("AWS::IAM::Policy")).flatMap(
      (policy) => (policy.Properties?.PolicyDocument?.Statement ?? []).filter(isJsonObject),
    );
    const roleMutations = allIamStatements.filter(isRoleLifecycle);
    expect(roleMutations).toEqual(iamStatements.filter(isRoleLifecycle));
    expect(roleMutations).toHaveLength(2);
    expect(JSON.stringify(roleMutations)).not.toContain('"iam:*"');
    expect(JSON.stringify(roleMutations)).not.toContain("iam:AttachRolePolicy");
    expect(JSON.stringify(roleMutations)).not.toContain("iam:PutRolePermissionsBoundary");
    expect(JSON.stringify(roleMutations)).not.toContain("iam:DeleteRolePermissionsBoundary");
  });

  it("never injects the REST API id into the API Lambda environment", () => {
    const template = synth(Provider.S3);
    const apiLambda = Object.values(template.findResources("AWS::Lambda::Function")).find(
      (resource) => resource.Properties?.Handler === "api_handler.handler",
    );
    const variables = apiLambda?.Properties?.Environment?.Variables ?? {};

    // The API definition embeds this Lambda's ARN, so reading the API id from
    // the environment would create a CloudFormation cycle. The control plane
    // reads it from the authenticated request context instead.
    expect(variables).not.toHaveProperty("CUSTOM_CONNECTOR_REST_API_ID");
    expect(variables).toHaveProperty("RUNTIME_ROLE_BOUNDARY_ARN");
  });

  it("injects service-derived Batch command inputs and exact framework roles", () => {
    const template = synth(Provider.QUICK);
    const orchestrator = Object.values(template.findResources("AWS::Lambda::Function")).find(
      (resource) => resource.Properties?.Handler === "job_orchestrator_handler.handler",
    );
    expect(orchestrator?.Properties?.Environment?.Variables).toEqual(
      expect.objectContaining({
        AWS_BATCH_JOB_QUEUE: expect.anything(),
        CUSTOM_CONNECTOR_API_ENDPOINT: expect.anything(),
        DESTINATION_MODE: "QUICK",
        INGESTION_BUCKET_NAME: expect.anything(),
        FRAMEWORK_EXECUTION_ROLE_ARN: expect.anything(),
      }),
    );
    const orchestratorVariables = orchestrator?.Properties?.Environment?.Variables ?? {};
    expect(orchestratorVariables).not.toHaveProperty("ENVIRONMENT");
    // The container job role is the per-connector runtime role persisted with
    // the connector, so no shared job-role ARN is configured any more.
    expect(orchestratorVariables).not.toHaveProperty("FRAMEWORK_JOB_ROLE_ARN");
  });

  it("adds encrypted DLQs, bounded retries, and retry-aware alarms", () => {
    const template = synth(Provider.S3);
    template.resourceCountIs("AWS::SQS::Queue", 3);
    template.hasResourceProperties("AWS::SQS::Queue", {
      KmsMasterKeyId: Match.anyValue(),
      MessageRetentionPeriod: 1209600,
    });
    template.hasResourceProperties("AWS::Lambda::EventSourceMapping", {
      BisectBatchOnFunctionError: true,
      DestinationConfig: { OnFailure: { Destination: Match.anyValue() } },
      MaximumRetryAttempts: 3,
    });
    template.hasResourceProperties("AWS::Events::Rule", {
      Targets: Match.arrayWith([
        Match.objectLike({
          DeadLetterConfig: { Arn: Match.anyValue() },
          RetryPolicy: { MaximumEventAgeInSeconds: 3600, MaximumRetryAttempts: 3 },
        }),
      ]),
    });
    template.resourceCountIs("AWS::CloudWatch::Alarm", 8);
    const alarms = Object.values(template.findResources("AWS::CloudWatch::Alarm"));
    for (const alarm of alarms) {
      expect(alarm.Properties?.AlarmDescription).toMatch(
        /^\[P[12]\].*docs\/runbooks\/delivery-failures\.md$/,
      );
      expect(alarm.Properties?.EvaluationPeriods).toBe(3);
      expect(alarm.Properties?.DatapointsToAlarm).toBe(2);
    }
    const schedulerAlarm = alarms.find(
      (alarm) =>
        alarm.Properties?.Namespace === "AWS/Scheduler" &&
        alarm.Properties?.MetricName === "TargetErrorCount",
    );
    expect(schedulerAlarm?.Properties).toEqual(
      expect.objectContaining({
        Namespace: "AWS/Scheduler",
        MetricName: "TargetErrorCount",
        Dimensions: [{ Name: "ScheduleGroup", Value: "CcfSchedulerGroup" }],
        Statistic: "Sum",
        Period: 300,
        Threshold: 0,
        ComparisonOperator: "GreaterThanThreshold",
        TreatMissingData: "notBreaching",
        EvaluationPeriods: 3,
        DatapointsToAlarm: 2,
      }),
    );
    const lambdaAlarms = alarms.filter((alarm) => alarm.Properties?.Namespace === "AWS/Lambda");
    expect(lambdaAlarms).toHaveLength(4);
    for (const alarm of lambdaAlarms) {
      expect(alarm.Properties?.TreatMissingData).toBe("notBreaching");
    }
  });

  it("grants Bedrock ingestion only to the Bedrock job-status Lambda", () => {
    const bedrockTemplate = synth(Provider.BEDROCK);
    const bedrockStatus = Object.values(
      bedrockTemplate.findResources("AWS::Lambda::Function"),
    ).find((resource) => resource.Properties?.Handler === "job_status_handler.handler");
    expect(bedrockStatus?.Properties?.Environment?.Variables).toEqual(
      expect.objectContaining({
        DESTINATION_MODE: "BEDROCK",
        BEDROCK_KNOWLEDGE_BASE_ID: expect.anything(),
        BEDROCK_DATA_SOURCE_ID: expect.anything(),
      }),
    );
    const bedrockStatements = Object.values(
      bedrockTemplate.findResources("AWS::IAM::Policy"),
    ).flatMap((policy) => policy.Properties?.PolicyDocument?.Statement ?? []);
    const ingestionStatement = bedrockStatements.find(
      (statement: { Action?: unknown }) => statement.Action === "bedrock:StartIngestionJob",
    );
    expect(JSON.stringify(ingestionStatement?.Resource)).toContain(":knowledge-base/");

    for (const provider of [Provider.S3, Provider.QUICK]) {
      const template = synth(provider);
      const status = Object.values(template.findResources("AWS::Lambda::Function")).find(
        (resource) => resource.Properties?.Handler === "job_status_handler.handler",
      );
      expect(status?.Properties?.Environment?.Variables).not.toHaveProperty(
        "BEDROCK_KNOWLEDGE_BASE_ID",
      );
      expect(status?.Properties?.Environment?.Variables).not.toHaveProperty(
        "BEDROCK_DATA_SOURCE_ID",
      );
      expect(JSON.stringify(template.findResources("AWS::IAM::Policy"))).not.toContain(
        "bedrock:StartIngestionJob",
      );
    }
  });

  it("does NOT create a Bedrock knowledge base for provider=s3", () => {
    const template = synth(Provider.S3);
    template.resourceCountIs("AWS::Bedrock::KnowledgeBase", 0);
    template.resourceCountIs("AWS::S3Vectors::VectorBucket", 0);
  });

  it("does NOT create a Bedrock knowledge base for provider=quick", () => {
    const template = synth(Provider.QUICK);
    template.resourceCountIs("AWS::Bedrock::KnowledgeBase", 0);
  });

  it("creates a Bedrock knowledge base on S3 Vectors for provider=bedrock", () => {
    const template = synth(Provider.BEDROCK);
    template.resourceCountIs("AWS::Bedrock::KnowledgeBase", 1);
    template.resourceCountIs("AWS::Bedrock::DataSource", 1);
    template.resourceCountIs("AWS::S3Vectors::VectorBucket", 1);
    template.resourceCountIs("AWS::S3Vectors::Index", 1);
  });
});

describe("CustomConnectorFrameworkStack security (cdk-nag AwsSolutionsChecks)", () => {
  const synthWithNag = (provider: Provider): { warnings: unknown[]; errors: unknown[] } => {
    const app = createApp();
    const stack = new CustomConnectorFrameworkStack(app, "NagStack", {
      projectName: ProjectName.CCF,
      environmentName: EnvironmentName.DEV,
      retainResources: false,
      account: FRAMEWORK_ACCOUNT,
      region: "us-east-1",
      allowedClientAccountIds: [CLIENT_ACCOUNT],
      provider,
      embeddingModelId: "amazon.titan-embed-text-v2:0",
      vectorDimension: 1024,
      env: { account: "123456789012", region: "us-east-1" },
    });
    Aspects.of(app).add(new AwsSolutionsChecks({ verbose: true }));
    if (NAG_SUPPRESSIONS.length > 0) {
      NagSuppressions.addStackSuppressions(stack, [...NAG_SUPPRESSIONS]);
    }
    const errors = Annotations.fromStack(stack).findError(
      "*",
      Match.stringLikeRegexp("AwsSolutions-.*"),
    );
    const warnings = Annotations.fromStack(stack).findWarning(
      "*",
      Match.stringLikeRegexp("AwsSolutions-.*"),
    );
    return { warnings, errors };
  };

  it("has no unsuppressed cdk-nag errors for provider=s3", () => {
    const { errors } = synthWithNag(Provider.S3);
    expect(errors).toHaveLength(0);
  });

  it("has no unsuppressed cdk-nag errors for provider=bedrock", () => {
    const { errors } = synthWithNag(Provider.BEDROCK);
    expect(errors).toHaveLength(0);
  });
});

describe("runtime-callable route metadata", () => {
  const generatedOpenApi = (): JsonObject => {
    const document: unknown = JSON.parse(
      readFileSync(resolve(__dirname, "../../api/generated/openapi.json"), "utf8"),
    );
    if (!isJsonObject(document)) {
      throw new Error("Generated OpenAPI must be an object.");
    }
    return document;
  };

  const markedRoutes = (): { method: string; path: string }[] => {
    const paths = generatedOpenApi().paths;
    if (!isJsonObject(paths)) {
      throw new Error("Generated OpenAPI must contain paths.");
    }
    return Object.entries(paths)
      .flatMap(([path, pathItem]) =>
        isJsonObject(pathItem)
          ? Object.entries(pathItem).flatMap(([method, operation]) =>
              isJsonObject(operation) && operation["x-ccf-runtime-callable"] === true
                ? [{ method: method.toUpperCase(), path }]
                : [],
            )
          : [],
      )
      .sort(
        (first, second) =>
          first.path.localeCompare(second.path) || first.method.localeCompare(second.method),
      );
  };

  it("derives the deployed route templates from the generated contract", () => {
    const template = synth(Provider.S3);
    const apiLambda = Object.values(template.findResources("AWS::Lambda::Function")).find(
      (resource) => resource.Properties?.Handler === "api_handler.handler",
    );
    const serializedRoutes = apiLambda?.Properties?.Environment?.Variables
      ?.CONNECTOR_RUNTIME_ROUTES as string | undefined;
    if (serializedRoutes === undefined) {
      throw new Error("Expected the API Lambda to receive generated runtime route templates.");
    }

    // One inventory only: whatever Smithy marks is what the control plane gets.
    expect(JSON.parse(serializedRoutes)).toEqual(markedRoutes());
  });

  it("marks exactly the connector-scoped operations a connector runtime needs", () => {
    const routes = markedRoutes();

    expect(routes.map((route) => `${route.method} ${route.path}`)).toEqual([
      "GET /api/v1/custom-connectors/{connector_id}",
      "DELETE /api/v1/custom-connectors/{connector_id}/checkpoint",
      "GET /api/v1/custom-connectors/{connector_id}/checkpoint",
      "PUT /api/v1/custom-connectors/{connector_id}/checkpoint",
      "DELETE /api/v1/custom-connectors/{connector_id}/documents",
      "GET /api/v1/custom-connectors/{connector_id}/documents",
      "POST /api/v1/custom-connectors/{connector_id}/documents",
      "GET /api/v1/custom-connectors/{connector_id}/jobs/{job_id}",
    ]);
    for (const route of routes) {
      expect(route.path).toContain("{connector_id}");
    }
    // Control-plane-only operations must never be runtime-callable.
    const runtimePaths = new Set(routes.map((route) => `${route.method} ${route.path}`));
    for (const forbidden of [
      "GET /api/v1/custom-connectors",
      "POST /api/v1/custom-connectors",
      "PUT /api/v1/custom-connectors/{connector_id}",
      "DELETE /api/v1/custom-connectors/{connector_id}",
      "POST /api/v1/custom-connectors/{connector_id}/jobs",
      "GET /api/v1/custom-connectors/{connector_id}/jobs",
      "POST /api/v1/custom-connectors/{connector_id}/jobs/{job_id}/stop",
    ]) {
      expect(runtimePaths).not.toContain(forbidden);
    }
  });

  it("passes only configured client accounts to the control plane", () => {
    const template = synth(Provider.S3);
    const apiLambda = Object.values(template.findResources("AWS::Lambda::Function")).find(
      (resource) => resource.Properties?.Handler === "api_handler.handler",
    );

    // Registration rejects source roles the shared boundary would deny.
    expect(apiLambda?.Properties?.Environment?.Variables?.ALLOWED_CLIENT_ACCOUNT_IDS).toEqual(
      CLIENT_ACCOUNT,
    );
  });
});
