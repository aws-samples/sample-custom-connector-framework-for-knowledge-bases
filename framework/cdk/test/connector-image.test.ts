import { mkdirSync, mkdtempSync, readdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Duration, RemovalPolicy, Stack, Stage } from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import { RetentionDays } from "aws-cdk-lib/aws-logs";
import { Asset } from "aws-cdk-lib/aws-s3-assets";
import {
  ConnectorImage,
  ConnectorImageProps,
  repositoryNameProblem,
} from "../lib/constructs/connector-image";
import {
  FRAMEWORK_LITERALS,
  IMAGE_DIRECTORY,
  JsonObject,
  literalFramework,
  onlyResourceId,
  propertiesOf,
  resourcesOf,
  statementsOf,
  tenantStack,
} from "./construct-fixtures";

interface Synthesized {
  readonly stack: Stack;
  readonly image: ConnectorImage;
  readonly template: Template;
}

const synthesize = (overrides: Partial<ConnectorImageProps> = {}): Synthesized => {
  const stack = tenantStack();
  const image = new ConnectorImage(stack, "Image", {
    framework: literalFramework(),
    repositoryName: "ccf-connectors/github",
    directory: IMAGE_DIRECTORY,
    file: "Dockerfile",
    removalPolicy: RemovalPolicy.RETAIN,
    maxImageAge: Duration.days(365),
    logRetention: RetentionDays.ONE_MONTH,
    buildTimeout: Duration.hours(1),
    ...overrides,
  });
  return { stack, image, template: Template.fromStack(stack) };
};

const buildProject = (template: Template): JsonObject =>
  template.toJSON().Resources[onlyResourceId(template, "AWS::CodeBuild::Project")] as JsonObject;

/** Each statement of the build role as `[actions, resources]`, sorted, without its `Sid`. */
const buildRoleGrants = (template: Template): string[] => {
  const [roleId] = Object.keys(
    template.findResources("AWS::IAM::Role", {
      Properties: { Description: Match.stringLikeRegexp("^Builds the connector image") },
    }),
  );
  return resourcesOf(template, "AWS::IAM::Policy")
    .filter((policy) => JSON.stringify(propertiesOf(policy).Roles).includes(roleId))
    .flatMap((policy) => statementsOf(propertiesOf(policy).PolicyDocument))
    .map((statement) =>
      JSON.stringify([[statement.Action].flat().map(String).sort(), statement.Resource]),
    )
    .sort();
};

describe("ConnectorImage's repository", () => {
  const { template } = synthesize();
  const [repository] = resourcesOf(template, "AWS::ECR::Repository");

  it("scans on push, keeps tags immutable, and keeps the given removal policy", () => {
    expect(propertiesOf(repository)).toMatchObject({
      RepositoryName: "ccf-connectors/github",
      ImageScanningConfiguration: { ScanOnPush: true },
      ImageTagMutability: "IMMUTABLE",
    });
    expect(repository.DeletionPolicy).toBe("Retain");
  });

  it("lets only the framework's Batch execution role pull", () => {
    expect(propertiesOf(repository).RepositoryPolicyText).toEqual({
      Version: "2012-10-17",
      Statement: [
        {
          Effect: "Allow",
          Principal: { AWS: FRAMEWORK_LITERALS.batchExecutionRoleArn },
          Action: [
            "ecr:BatchCheckLayerAvailability",
            "ecr:BatchGetImage",
            "ecr:GetDownloadUrlForLayer",
          ],
        },
      ],
    });
  });

  it("expires every image at the given age after its push", () => {
    const policy = JSON.parse(
      String((propertiesOf(repository).LifecyclePolicy as JsonObject).LifecyclePolicyText),
    ) as { rules: JsonObject[] };

    expect(policy.rules).toEqual([
      expect.objectContaining({
        selection: {
          tagStatus: "any",
          countType: "sinceImagePushed",
          countNumber: 365,
          countUnit: "days",
        },
        action: { type: "expire" },
      }),
    ]);
  });

  it("expires images at the configured age", () => {
    const [shortLived] = resourcesOf(
      synthesize({ maxImageAge: Duration.days(14) }).template,
      "AWS::ECR::Repository",
    );

    expect(
      String((propertiesOf(shortLived).LifecyclePolicy as JsonObject).LifecyclePolicyText),
    ).toContain('"countNumber":14');
  });

  it("is deleted with its images under DESTROY", () => {
    const [destroyed] = resourcesOf(
      synthesize({ removalPolicy: RemovalPolicy.DESTROY }).template,
      "AWS::ECR::Repository",
    );

    expect(destroyed.DeletionPolicy).toBe("Delete");
    expect(propertiesOf(destroyed).EmptyOnDelete).toBe(true);
  });

  it("fails synthesis for a name outside ccf-connectors/, naming repositoryName", () => {
    expect(() => synthesize({ repositoryName: "github" })).toThrow(/repositoryName/);
  });

  it("names the problem with a repository name outside ccf-connectors/", () => {
    expect(repositoryNameProblem("ccf-connectors/github")).toBeUndefined();
    expect(repositoryNameProblem("github")).toContain('repositoryName "github" must start with');
  });
});

describe("ConnectorImage's source asset", () => {
  const assetFiles = (directory: string): string[] => {
    const { image } = synthesize({ directory, file: "backend/Dockerfile" });
    const [asset] = image.node.findAll().filter((child): child is Asset => child instanceof Asset);
    const staged = join(Stage.of(image)?.outdir ?? "", asset.assetPath);
    return readdirSync(staged, { recursive: true }).map(String).sort();
  };

  it("leaves out what the Dockerfile's own .dockerignore lists, as docker build -f does", () => {
    const directory = mkdtempSync(join(tmpdir(), "ccf-image-"));
    mkdirSync(join(directory, "backend"));
    mkdirSync(join(directory, "notes"));
    writeFileSync(join(directory, "backend", "Dockerfile"), "FROM scratch\n");
    writeFileSync(join(directory, "backend", "Dockerfile.dockerignore"), "notes\n");
    writeFileSync(join(directory, "backend", "main.py"), "print()\n");
    writeFileSync(join(directory, "notes", "draft.md"), "draft\n");

    const files = assetFiles(directory);

    expect(files).toEqual(
      expect.arrayContaining([join("backend", "Dockerfile"), join("backend", "main.py")]),
    );
    expect(files.filter((name) => name.startsWith("notes"))).toEqual([]);
  });
});

describe("ConnectorImage's build", () => {
  const { stack, image, template } = synthesize();
  const project = buildProject(template);

  it("builds on arm64 in the version-tagged Amazon Linux 2023 image, privileged for Docker", () => {
    expect(propertiesOf(project).Environment).toMatchObject({
      Type: "ARM_CONTAINER",
      Image: "aws/codebuild/amazonlinux-aarch64-standard:3.0",
      ComputeType: "BUILD_GENERAL1_SMALL",
      PrivilegedMode: true,
    });
  });

  it("builds for linux/arm64 and pushes the image by its tag", () => {
    const buildSpec = String((propertiesOf(project).Source as JsonObject).BuildSpec);

    expect(buildSpec).toContain(
      'docker build --platform linux/arm64 -f \\"$DOCKERFILE\\" -t \\"$IMAGE_URI\\" .',
    );
    expect(buildSpec).toContain('docker push \\"$IMAGE_URI\\"');
    expect(buildSpec).not.toContain("@sha256");
  });

  it("tags the image with the source asset's hash, never a digest", () => {
    expect(stack.resolve(image.imageUri)).toEqual(
      expect.objectContaining({
        "Fn::Join": expect.arrayContaining([expect.arrayContaining([`:${image.imageTag}`])]),
      }),
    );
    expect(image.imageTag).toMatch(/^[0-9a-f]{64}$/);
  });

  it("gives the build role exactly push to its repository, the source object, and its log group", () => {
    const repositoryArn = {
      "Fn::GetAtt": [onlyResourceId(template, "AWS::ECR::Repository"), "Arn"],
    };
    const grants = buildRoleGrants(template);

    expect(grants).toHaveLength(4);
    expect(grants).toEqual(
      expect.arrayContaining([
        JSON.stringify([["ecr:GetAuthorizationToken"], "*"]),
        JSON.stringify([
          [
            "ecr:BatchCheckLayerAvailability",
            "ecr:CompleteLayerUpload",
            "ecr:InitiateLayerUpload",
            "ecr:PutImage",
            "ecr:UploadLayerPart",
          ],
          repositoryArn,
        ]),
      ]),
    );
    expect(grants.find((grant) => grant.includes("s3:GetObject"))).toMatch(/\.zip/);
    expect(grants.find((grant) => grant.includes("logs:PutLogEvents"))).toMatch(
      /\["logs:CreateLogStream","logs:PutLogEvents"\].*BuildLogs/,
    );
  });

  it("puts no inline policy on the build role, which the project can't add to", () => {
    const [role] = resourcesOf(template, "AWS::IAM::Role").filter((candidate) =>
      String(propertiesOf(candidate).Description).startsWith("Builds the connector image"),
    );

    expect(propertiesOf(role).Policies).toBeUndefined();
    expect(JSON.stringify(propertiesOf(project).ServiceRole)).toContain("BuildRole");
  });

  it("starts the build from a custom resource after the repository and project exist", () => {
    const [resource] = Object.values(
      template.findResources("Custom::CcfConnectorImage"),
    ) as JsonObject[];
    const projectId = onlyResourceId(template, "AWS::CodeBuild::Project");
    const repositoryId = onlyResourceId(template, "AWS::ECR::Repository");

    expect(propertiesOf(resource)).toMatchObject({
      ProjectName: { Ref: projectId },
      RepositoryName: { Ref: repositoryId },
      ImageTag: image.imageTag,
    });
    expect(resource.DependsOn).toEqual(expect.arrayContaining([repositoryId]));
  });

  it("lets the provider describe images in its own repository only", () => {
    const repositoryId = onlyResourceId(template, "AWS::ECR::Repository");
    const describes = resourcesOf(template, "AWS::IAM::Policy").flatMap((policy) =>
      statementsOf(propertiesOf(policy).PolicyDocument)
        .filter((statement) => JSON.stringify(statement.Action).includes("ecr:DescribeImages"))
        .map((statement) => [statement.Effect, statement.Action, statement.Resource]),
    );

    expect(describes).toEqual([
      ["Allow", "ecr:DescribeImages", { "Fn::GetAtt": [repositoryId, "Arn"] }],
    ]);
  });

  it("lets the provider start and read builds of its own project only", () => {
    const projectId = onlyResourceId(template, "AWS::CodeBuild::Project");
    const actions = resourcesOf(template, "AWS::IAM::Policy").flatMap((policy) =>
      statementsOf(propertiesOf(policy).PolicyDocument)
        .filter((statement) => String(statement.Action).startsWith("codebuild:"))
        .map((statement) => [statement.Action, statement.Resource]),
    );

    expect(actions).toEqual(
      expect.arrayContaining([
        ["codebuild:StartBuild", { "Fn::GetAtt": [projectId, "Arn"] }],
        ["codebuild:BatchGetBuilds", { "Fn::GetAtt": [projectId, "Arn"] }],
      ]),
    );
    expect(actions).toHaveLength(2);
  });
});

describe("ConnectorImage.fromRepositoryName", () => {
  it("names the image by repository and tag and creates nothing", () => {
    const stack = tenantStack();
    const imported = ConnectorImage.fromRepositoryName(stack, "Imported", {
      repositoryName: "ccf-connectors/github",
      imageTag: "1.0.0",
    });

    expect(JSON.stringify(stack.resolve(imported.imageUri))).toContain(
      "ccf-connectors/github:1.0.0",
    );
    expect(Template.fromStack(stack).toJSON().Resources ?? {}).toEqual({});
  });

  it("rejects a repository outside ccf-connectors/", () => {
    expect(() =>
      ConnectorImage.fromRepositoryName(tenantStack(), "Imported", {
        repositoryName: "github",
        imageTag: "1.0.0",
      }),
    ).toThrow(/repositoryName/);
  });
});
