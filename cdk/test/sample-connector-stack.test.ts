import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { App, Aspects, Fn } from "aws-cdk-lib";
import { Annotations, Match, Template } from "aws-cdk-lib/assertions";
import { AwsSolutionsChecks } from "cdk-nag";
import { applySampleNagSuppressions } from "../lib/common/sample-nag-suppressions";
import { SAMPLE_CONNECTOR_DEFINITIONS, SampleName, synthSampleStack } from "../lib/sample-app";
import { ClientImageAssetsFoundationStack } from "../lib/stacks/client-image-assets-foundation-stack";
import {
  CustomConnectorFrameworkClientStack,
  REGISTRATION_BUNDLE_COMMAND,
  REGISTRATION_BUNDLE_PLATFORM,
} from "../lib/stacks/custom-connector-framework-client-stack";

const FRAMEWORK_ACCOUNT = "123456789012";
const CLIENT_ACCOUNT = "210987654321";
const FRAMEWORK_EXECUTION_PRINCIPAL_ARN = `arn:aws:iam::${FRAMEWORK_ACCOUNT}:role/CcfFrameworkExecutionRole`;
const SOURCE_EXTERNAL_ID = "ccf-test-external-id";
const GITHUB_SECRET_ARN = `arn:aws:secretsmanager:us-east-1:${CLIENT_ACCOUNT}:secret:github-token-Ab12Cd`;
const GITLAB_SECRET_ARN = `arn:aws:secretsmanager:us-east-1:${CLIENT_ACCOUNT}:secret:gitlab-token-Ef34Gh`;
const GITHUB_REPOSITORY = "octocat/Hello-World";
const GITLAB_PROJECT = "group/project";
const REPOSITORY_NAME = `ccf-client-image-assets-${CLIENT_ACCOUNT}-us-east-1`;
const CLIENT_STACK_SOURCE = resolve(
  __dirname,
  "../lib/stacks/custom-connector-framework-client-stack.ts",
);
const SERVICE_REFERENCE_SOURCE = resolve(__dirname, "../lib/common/ccf-service-reference.ts");
const BUNDLING_DOCKERFILE = resolve(__dirname, "../lambdas/connector_ops/Dockerfile");
const EXPECTED_BUNDLE_COMMAND = [
  "uv export --quiet --project /asset-input/cdk --frozen --no-dev --no-emit-project --no-emit-local --prune filetype --output-file /tmp/requirements.txt",
  "uv pip install --quiet --python /usr/local/bin/python --target /asset-output --require-hashes --no-deps --no-cache -r /tmp/requirements.txt",
  "cp -R /asset-input/api/packages/ccf-api-models/src/ccf_api_models /asset-output/",
  "mkdir -p /asset-output/custom_connector_framework",
  "cp /asset-input/toolkit/src/custom_connector_framework/client.py /asset-input/toolkit/src/custom_connector_framework/_generated_client.py /asset-output/custom_connector_framework/",
  "cp /asset-input/cdk/lambdas/connector_ops/runtime_package_init.py /asset-output/custom_connector_framework/__init__.py",
  "cp /asset-input/cdk/lambdas/connector_ops/ccf.py /asset-input/cdk/lambdas/connector_ops/register.py /asset-output/",
  "PYTHONPATH=/asset-output python -c 'import ccf_api_models, custom_connector_framework.client, register'",
].join(" && ");

const EXPECTED_COMMANDS = {
  [SampleName.WEB_CRAWLER]: ["ccf-connector", "run", "web_crawler.connector:definition"],
  [SampleName.GITHUB]: ["ccf-connector", "run", "github_connector.connector:definition"],
  [SampleName.GITLAB]: ["ccf-connector", "run", "gitlab_connector.connector:definition"],
} satisfies Record<SampleName, readonly string[]>;

const EXPECTED_CONFIGURATIONS = {
  [SampleName.WEB_CRAWLER]: { urls: [{ url: "https://example.com" }] },
  [SampleName.GITHUB]: {
    token_secret_arn: GITHUB_SECRET_ARN,
    repositories: [{ full_name: GITHUB_REPOSITORY }],
  },
  [SampleName.GITLAB]: {
    token_secret_arn: GITLAB_SECRET_ARN,
    projects: [{ project_id: GITLAB_PROJECT }],
  },
} satisfies Record<SampleName, Readonly<Record<string, unknown>>>;

type JsonObject = Record<string, unknown>;
interface SampleFixture {
  readonly app: App;
  readonly foundation: ClientImageAssetsFoundationStack;
  readonly sample: CustomConnectorFrameworkClientStack;
}

const isJsonObject = (value: unknown): value is JsonObject =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const executeApiArn = (methodAndPath: string): JsonObject => ({
  "Fn::Join": [
    "",
    [
      "arn:",
      { Ref: "AWS::Partition" },
      `:execute-api:us-east-1:${FRAMEWORK_ACCOUNT}:abc123/*/${methodAndPath}`,
    ],
  ],
});

const fixture = (
  sampleName: SampleName,
  contextOverrides: Readonly<Record<string, unknown>> = {},
): SampleFixture => {
  const app = new App({
    context: {
      "aws:cdk:bundling-stacks": [],
      sample: sampleName,
      apiEndpoint: "https://abc123.execute-api.us-east-1.amazonaws.com/prod/",
      frameworkRestApiId: "abc123",
      frameworkRegion: "us-east-1",
      frameworkAccount: FRAMEWORK_ACCOUNT,
      frameworkExecutionPrincipalArn: FRAMEWORK_EXECUTION_PRINCIPAL_ARN,
      sourceAccessExternalId: SOURCE_EXTERNAL_ID,
      tokenSecretArn: sampleName === SampleName.GITLAB ? GITLAB_SECRET_ARN : GITHUB_SECRET_ARN,
      repositoryFullName: GITHUB_REPOSITORY,
      projectId: GITLAB_PROJECT,
      ...contextOverrides,
    },
  });
  expect(synthSampleStack(app, CLIENT_ACCOUNT, "us-east-1")).toBe(true);
  const foundation = app.node.findChild("ClientImageAssetsFoundationStack");
  const sample = app.node.findChild(SAMPLE_CONNECTOR_DEFINITIONS[sampleName].stackId);
  if (
    !(foundation instanceof ClientImageAssetsFoundationStack) ||
    !(sample instanceof CustomConnectorFrameworkClientStack)
  )
    throw new Error("Expected foundation and sample client stacks.");
  return { app, foundation, sample };
};

const registration = (template: Template, connectorName: string): JsonObject => {
  const properties = Object.values(
    template.findResources("AWS::CloudFormation::CustomResource"),
  ).find((resource) => resource.Properties?.ConnectorName === connectorName)?.Properties;
  if (!isJsonObject(properties)) throw new Error(`Missing registration for ${connectorName}.`);
  return properties;
};

const statementsForRoleReference = (template: Template, roleLogicalId: string): JsonObject[] =>
  Object.values(template.findResources("AWS::IAM::Policy"))
    .filter((policy) =>
      (policy.Properties?.Roles ?? []).some(
        (reference: unknown) => isJsonObject(reference) && reference.Ref === roleLogicalId,
      ),
    )
    .flatMap((policy) => (policy.Properties?.PolicyDocument?.Statement ?? []).filter(isJsonObject));

const roleStatements = (template: Template, roleName: string): JsonObject[] => {
  const role = Object.entries(template.findResources("AWS::IAM::Role")).find(
    ([, resource]) => resource.Properties?.RoleName === roleName,
  );
  if (role === undefined) throw new Error(`Missing role ${roleName}.`);
  return statementsForRoleReference(template, role[0]);
};

const lambdaRoleStatements = (template: Template, handler: string): JsonObject[] => {
  const lambda = Object.values(template.findResources("AWS::Lambda::Function")).find(
    (resource) => resource.Properties?.Handler === handler,
  );
  const role = lambda?.Properties?.Role;
  if (!isJsonObject(role) || !Array.isArray(role["Fn::GetAtt"]))
    throw new Error(`Missing Lambda role for ${handler}.`);
  const roleLogicalId = role["Fn::GetAtt"];
  if (!Array.isArray(roleLogicalId) || typeof roleLogicalId[0] !== "string")
    throw new Error(`Malformed Lambda role for ${handler}.`);
  return statementsForRoleReference(template, roleLogicalId[0]);
};

const allActions = (template: Template): string[] =>
  Object.values(template.findResources("AWS::IAM::Policy")).flatMap((policy) =>
    (policy.Properties?.PolicyDocument?.Statement ?? []).flatMap((statement: JsonObject) => {
      if (typeof statement.Action === "string") return [statement.Action];
      return Array.isArray(statement.Action)
        ? statement.Action.filter((action): action is string => typeof action === "string")
        : [];
    }),
  );

describe("sample connector composition", () => {
  it.each([
    [SampleName.WEB_CRAWLER, "SampleWebCrawlerStack"],
    [SampleName.GITHUB, "SampleGitHubStack"],
    [SampleName.GITLAB, "SampleGitLabStack"],
  ])("creates %s with exact id %s after its foundation", (sampleName, stackId) => {
    const { foundation, sample } = fixture(sampleName);
    expect(sample.node.id).toBe(stackId);
    expect(sample.dependencies).toContain(foundation);
  });

  it.each(Object.values(SampleName))("defines the exact %s launcher command", (sampleName) => {
    expect(SAMPLE_CONNECTOR_DEFINITIONS[sampleName].command).toEqual(EXPECTED_COMMANDS[sampleName]);
  });

  it("publishes Docker image assets to the dedicated repository", () => {
    const { app, sample } = fixture(SampleName.WEB_CRAWLER);
    const assembly = app.synth();
    const assetArtifact = Object.entries(assembly.manifest.artifacts ?? {}).find(
      ([artifactId, artifact]) =>
        artifact.type === "cdk:asset-manifest" && artifactId.startsWith(sample.artifactId),
    )?.[1];
    if (
      !isJsonObject(assetArtifact?.properties) ||
      typeof assetArtifact.properties.file !== "string"
    )
      throw new Error("Expected the sample asset manifest.");
    const manifest: unknown = JSON.parse(
      readFileSync(resolve(assembly.directory, assetArtifact.properties.file), "utf8"),
    );
    if (!isJsonObject(manifest) || !isJsonObject(manifest.dockerImages))
      throw new Error("Expected Docker images in the sample asset manifest.");
    const connectorImage = Object.values(manifest.dockerImages).find(
      (image) => isJsonObject(image) && image.displayName === "ConnectorImage",
    );
    if (!isJsonObject(connectorImage)) throw new Error("Expected the connector Docker image.");
    const source = connectorImage.source;
    const destinations = connectorImage.destinations;
    if (!isJsonObject(source) || !isJsonObject(destinations))
      throw new Error("Expected connector Docker image source and destinations.");
    expect(source).toEqual(
      expect.objectContaining({ dockerFile: "samples/Dockerfile", platform: "linux/amd64" }),
    );
    if (typeof source.directory !== "string")
      throw new Error("Expected the connector Docker image source directory.");
    const stagedSourceDirectory = resolve(assembly.directory, source.directory);
    for (const requiredPath of [
      "api/packages/ccf-api-models/pyproject.toml",
      "toolkit/pyproject.toml",
      "samples/pyproject.toml",
      "samples/Dockerfile",
    ])
      expect(existsSync(resolve(stagedSourceDirectory, requiredPath))).toBe(true);
    for (const forbiddenPath of ["api/model", "backend", "cdk", ".git", ".env"])
      expect(existsSync(resolve(stagedSourceDirectory, forbiddenPath))).toBe(false);
    expect(Object.values(destinations).filter(isJsonObject)).toContainEqual(
      expect.objectContaining({ repositoryName: REPOSITORY_NAME }),
    );
  });
});

describe("client image repository foundation", () => {
  it("bundling-disabled foundation template has one application resource", () => {
    const template = Template.fromStack(fixture(SampleName.WEB_CRAWLER).foundation);
    expect(Object.keys(template.toJSON().Resources)).toHaveLength(1);
    template.resourceCountIs("AWS::ECR::Repository", 1);
    template.hasResourceProperties("AWS::ECR::Repository", {
      RepositoryName: REPOSITORY_NAME,
      ImageScanningConfiguration: { ScanOnPush: true },
      ImageTagMutability: "IMMUTABLE",
      LifecyclePolicy: Match.absent(),
      RepositoryPolicyText: {
        Statement: [
          {
            Action: [
              "ecr:BatchCheckLayerAvailability",
              "ecr:GetDownloadUrlForLayer",
              "ecr:BatchGetImage",
            ],
            Effect: "Allow",
            Principal: { AWS: FRAMEWORK_EXECUTION_PRINCIPAL_ARN },
          },
        ],
        Version: "2012-10-17",
      },
    });
  });

  it("has no unsuppressed cdk-nag errors", () => {
    const stack = fixture(SampleName.WEB_CRAWLER).foundation;
    Aspects.of(stack).add(new AwsSolutionsChecks({ verbose: true }));
    expect(
      Annotations.fromStack(stack).findError("*", Match.stringLikeRegexp("AwsSolutions-.*")),
    ).toHaveLength(0);
  });
});

describe("sample client boundary", () => {
  it.each(Object.values(SampleName))(
    "%s has one registration resource, one Provider, and no policy mutation",
    (sampleName) => {
      const template = Template.fromStack(fixture(sampleName).sample);
      template.resourceCountIs("AWS::CloudFormation::CustomResource", 1);
      const handlers = Object.values(template.findResources("AWS::Lambda::Function")).map(
        (resource) => resource.Properties?.Handler,
      );
      expect(handlers.filter((handler) => handler === "register.handler")).toHaveLength(1);
      expect(handlers.filter((handler) => handler === "framework.onEvent")).toHaveLength(1);
      expect(handlers).not.toContain("router.handler");
      expect(allActions(template)).not.toEqual(
        expect.arrayContaining([
          "ecr:GetRepositoryPolicy",
          "ecr:SetRepositoryPolicy",
          "ecr:DeleteRepositoryPolicy",
        ]),
      );
      for (const resourceType of [
        "AWS::Scheduler::Schedule",
        "AWS::Batch::JobDefinition",
        "AWS::DynamoDB::Table",
        "AWS::S3::Bucket",
        "AWS::SQS::Queue",
        "AWS::KMS::Key",
        "AWS::SSM::Parameter",
      ])
        template.resourceCountIs(resourceType, 0);
    },
  );

  it.each([
    [SampleName.WEB_CRAWLER, 9, 10, 17],
    [SampleName.GITHUB, 10, 11, 18],
    [SampleName.GITLAB, 10, 11, 18],
  ])(
    "%s uses %i sample resources and %i effective first-use resources instead of %i",
    (sampleName, sampleCount, firstUseTotal, oldTotal) => {
      const { foundation, sample } = fixture(sampleName);
      const foundationCount = Object.keys(Template.fromStack(foundation).toJSON().Resources).length;
      const localSampleCount = Object.keys(Template.fromStack(sample).toJSON().Resources).length;
      expect(foundationCount).toBe(1);
      expect(localSampleCount).toBe(sampleCount);
      expect(foundationCount + localSampleCount).toBe(firstUseTotal);
      expect(firstUseTotal).toBeLessThan(oldTotal);
    },
  );

  it("rejects an exact resolved service and client Region mismatch", () => {
    expect(() => fixture(SampleName.WEB_CRAWLER, { frameworkRegion: "us-west-2" })).toThrow(
      /^CCF service Region "us-west-2" must match client stack Region "us-east-1"\.$/,
    );
  });

  it("preserves unresolved service and source-secret tokens", () => {
    const template = Template.fromStack(
      fixture(SampleName.GITHUB, {
        frameworkRegion: Fn.importValue("FrameworkRegion"),
        tokenSecretArn: Fn.importValue("TokenSecretArn"),
      }).sample,
    );
    const serialized = JSON.stringify(template.toJSON());
    expect(serialized).toContain('"Fn::ImportValue":"FrameworkRegion"');
    expect(serialized).toContain('"Fn::ImportValue":"TokenSecretArn"');
  });

  it("rejects invented partitions and malformed source-secret Regions", () => {
    expect(() =>
      fixture(SampleName.WEB_CRAWLER, {
        frameworkExecutionPrincipalArn: `arn:aws-invented:iam::${FRAMEWORK_ACCOUNT}:role/CcfFrameworkExecutionRole`,
      }),
    ).toThrow(/client stack ARN is malformed/);
    for (const tokenSecretArn of [
      `arn:aws-invented:secretsmanager:us-east-1:${CLIENT_ACCOUNT}:secret:github-token-Ab12Cd`,
      `arn:aws:secretsmanager:us_east_1:${CLIENT_ACCOUNT}:secret:github-token-Ab12Cd`,
      `arn:aws:secretsmanager:us--east-1:${CLIENT_ACCOUNT}:secret:github-token-Ab12Cd`,
      `arn:aws:secretsmanager:us-123-east-1:${CLIENT_ACCOUNT}:secret:github-token-Ab12Cd`,
      `arn:aws:secretsmanager:us-east-one:${CLIENT_ACCOUNT}:secret:github-token-Ab12Cd`,
    ])
      expect(() => fixture(SampleName.GITHUB, { tokenSecretArn })).toThrow(
        /client stack ARN is malformed/,
      );
  });

  it("rejects valid framework and source-secret ARNs from another partition", () => {
    expect(() =>
      fixture(SampleName.WEB_CRAWLER, {
        frameworkExecutionPrincipalArn: `arn:aws-cn:iam::${FRAMEWORK_ACCOUNT}:role/CcfFrameworkExecutionRole`,
      }),
    ).toThrow(/client stack ARN is malformed/);
    expect(() =>
      fixture(SampleName.GITHUB, {
        tokenSecretArn: `arn:aws-cn:secretsmanager:us-east-1:${CLIENT_ACCOUNT}:secret:github-token-Ab12Cd`,
      }),
    ).toThrow(/client stack ARN is malformed/);
  });

  it("fails closed for other resolved malformed principal and source-secret ARNs", () => {
    expect(() =>
      fixture(SampleName.WEB_CRAWLER, {
        frameworkExecutionPrincipalArn: `arn:aws:iam::${FRAMEWORK_ACCOUNT}:role/OtherRole`,
      }),
    ).toThrow(/client stack ARN is malformed/);
    for (const tokenSecretArn of [
      `arn:aws:secretsmanager:us-east-1:${CLIENT_ACCOUNT}:secret:github-token`,
      `arn:aws:secretsmanager:us-west-2:${CLIENT_ACCOUNT}:secret:github-token-Ab12Cd`,
      "arn:aws:secretsmanager:us-east-1:999999999999:secret:github-token-Ab12Cd",
      `arn:aws:secretsmanager:us-east-1:${CLIENT_ACCOUNT}:secret:github-token-*`,
    ])
      expect(() => fixture(SampleName.GITHUB, { tokenSecretArn })).toThrow(
        /client stack ARN is malformed/,
      );
  });

  it("trusts only CCF connector runtime roles with this connector's ExternalId", () => {
    const template = Template.fromStack(fixture(SampleName.WEB_CRAWLER).sample);

    // Connector containers run with the service-managed per-connector runtime
    // role, whose name CCF mints at registration time. Trust therefore names the
    // service account plus the service-owned runtime-role path, and still
    // requires this connector's external ID.
    template.hasResourceProperties("AWS::IAM::Role", {
      RoleName: "CcfSourceRole-web-crawler",
      AssumeRolePolicyDocument: {
        Statement: [
          {
            Action: "sts:AssumeRole",
            Condition: {
              ArnLike: {
                "aws:PrincipalArn": {
                  "Fn::Join": [
                    "",
                    [
                      "arn:",
                      { Ref: "AWS::Partition" },
                      `:iam::${FRAMEWORK_ACCOUNT}:role/ccf-runtime/CcfConnectorRuntime-*`,
                    ],
                  ],
                },
              },
              StringEquals: { "sts:ExternalId": SOURCE_EXTERNAL_ID },
            },
            Effect: "Allow",
            Principal: {
              AWS: {
                "Fn::Join": [
                  "",
                  ["arn:", { Ref: "AWS::Partition" }, `:iam::${FRAMEWORK_ACCOUNT}:root`],
                ],
              },
            },
          },
        ],
        Version: "2012-10-17",
      },
    });

    // The Batch execution principal is never trusted to assume source access.
    const sourceRole = Object.values(template.findResources("AWS::IAM::Role")).find(
      (resource) => resource.Properties?.RoleName === "CcfSourceRole-web-crawler",
    );
    const trustPolicy = JSON.stringify(sourceRole?.Properties?.AssumeRolePolicyDocument);
    expect(trustPolicy).not.toContain("CcfFrameworkExecutionRole");
    expect(trustPolicy).not.toContain(":iam::*:");
    expect(trustPolicy).toContain("sts:ExternalId");
  });

  it.each([
    [SampleName.GITHUB, "github", GITHUB_SECRET_ARN],
    [SampleName.GITLAB, "gitlab", GITLAB_SECRET_ARN],
  ])(
    "%s grants its source role only its exact token secret",
    (sampleName, connectorName, secretArn) => {
      expect(
        roleStatements(
          Template.fromStack(fixture(sampleName).sample),
          `CcfSourceRole-${connectorName}`,
        ),
      ).toEqual([
        { Action: "secretsmanager:GetSecretValue", Effect: "Allow", Resource: secretArn },
      ]);
    },
  );

  it("grants no Secrets Manager permissions to crawler source role", () => {
    expect(
      roleStatements(
        Template.fromStack(fixture(SampleName.WEB_CRAWLER).sample),
        "CcfSourceRole-web-crawler",
      ),
    ).toEqual([]);
  });

  it.each(Object.values(SampleName))(
    "%s registration role has only exact create, update, and delete API routes",
    (sampleName) => {
      const statements = lambdaRoleStatements(
        Template.fromStack(fixture(sampleName).sample),
        "register.handler",
      );
      expect(statements).toEqual([
        {
          Action: "execute-api:Invoke",
          Effect: "Allow",
          Resource: [
            executeApiArn("POST/api/v1/custom-connectors"),
            executeApiArn("PUT/api/v1/custom-connectors/*"),
            executeApiArn("DELETE/api/v1/custom-connectors/*"),
          ],
        },
      ]);
    },
  );

  it.each(Object.values(SampleName))(
    "%s passes the exact typed configuration object through CloudFormation",
    (sampleName) => {
      const properties = registration(Template.fromStack(fixture(sampleName).sample), sampleName);
      expect(properties.ConnectorConfiguration).toEqual(EXPECTED_CONFIGURATIONS[sampleName]);
      expect(properties).not.toHaveProperty("ConnectorConfigurationSecretArn");
      expect(typeof properties.ConnectorConfiguration).toBe("object");
      expect(properties.Command).toEqual(EXPECTED_COMMANDS[sampleName]);
    },
  );

  it("adds only configured optional source endpoints", () => {
    const github = registration(
      Template.fromStack(
        fixture(SampleName.GITHUB, { githubApiUrl: "https://github.example/api/v3" }).sample,
      ),
      SampleName.GITHUB,
    );
    const gitlab = registration(
      Template.fromStack(
        fixture(SampleName.GITLAB, { gitlabServerUrl: "https://gitlab.example" }).sample,
      ),
      SampleName.GITLAB,
    );
    expect(github.ConnectorConfiguration).toEqual({
      ...EXPECTED_CONFIGURATIONS[SampleName.GITHUB],
      api_url: "https://github.example/api/v3",
    });
    expect(gitlab.ConnectorConfiguration).toEqual({
      ...EXPECTED_CONFIGURATIONS[SampleName.GITLAB],
      server_url: "https://gitlab.example",
    });
  });

  it("keeps Provider application logging at ERROR", () => {
    Template.fromStack(fixture(SampleName.WEB_CRAWLER).sample).hasResourceProperties(
      "AWS::Lambda::Function",
      {
        Handler: "framework.onEvent",
        LoggingConfig: {
          ApplicationLogLevel: "ERROR",
          LogFormat: "JSON",
        },
      },
    );
  });

  it("uses exact API coordinates and the reduced service reference", () => {
    const template = Template.fromStack(fixture(SampleName.WEB_CRAWLER).sample);
    expect(allActions(template).filter((action) => action === "execute-api:Invoke")).toHaveLength(
      1,
    );
    const source = readFileSync(SERVICE_REFERENCE_SOURCE, "utf8");
    expect(source).toContain("serviceAccountId");
    expect(source).toContain("frameworkExecutionPrincipalArn");
    expect(source).not.toMatch(/jobRoleArn|executionRoleArn|readonly account:/);
  });

  it("pins Lambda and bundling to x86_64 with the exact frozen command", () => {
    const template = Template.fromStack(fixture(SampleName.WEB_CRAWLER).sample);
    template.hasResourceProperties("AWS::Lambda::Function", {
      Handler: "register.handler",
      Architectures: ["x86_64"],
    });
    expect(REGISTRATION_BUNDLE_PLATFORM).toBe("linux/amd64");
    expect(REGISTRATION_BUNDLE_COMMAND).toBe(EXPECTED_BUNDLE_COMMAND);
    const source = readFileSync(CLIENT_STACK_SOURCE, "utf8");
    expect(source).toContain("platform: REGISTRATION_BUNDLE_PLATFORM");
    expect(source).toContain('command: ["bash", "-c", REGISTRATION_BUNDLE_COMMAND]');
    expect(source).not.toContain("cp -R /asset-input/toolkit/src/custom_connector_framework");
    expect(source).not.toMatch(/["']pip install/);
    expect(readFileSync(BUNDLING_DOCKERFILE, "utf8").match(/^FROM /gm)).toHaveLength(2);
  });
});

describe("sample connector stack security", () => {
  it.each(Object.values(SampleName))("%s has no unsuppressed cdk-nag errors", (sampleName) => {
    const stack = fixture(sampleName).sample;
    applySampleNagSuppressions(stack);
    Aspects.of(stack).add(new AwsSolutionsChecks({ verbose: true }));
    expect(
      Annotations.fromStack(stack).findError("*", Match.stringLikeRegexp("AwsSolutions-.*")),
    ).toHaveLength(0);
  });
});
