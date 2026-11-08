import { existsSync, readFileSync } from "node:fs";
import * as path from "node:path";
import { Aws, CustomResource, Duration, IgnoreMode, RemovalPolicy } from "aws-cdk-lib";
import {
  BuildSpec,
  ComputeType,
  LinuxArmBuildImage,
  Project,
  Source,
} from "aws-cdk-lib/aws-codebuild";
import { IRepository, Repository, TagMutability, TagStatus } from "aws-cdk-lib/aws-ecr";
import { ArnPrincipal, PolicyStatement, Role, ServicePrincipal } from "aws-cdk-lib/aws-iam";
import { LogGroup, RetentionDays } from "aws-cdk-lib/aws-logs";
import { Asset } from "aws-cdk-lib/aws-s3-assets";
import { Construct, IConstruct } from "constructs";
import {
  acknowledgeNagRules,
  CONNECTOR_IMAGE_ACKNOWLEDGMENTS,
  CONNECTOR_IMAGE_WILDCARD_REASON,
  resourceFindings,
} from "../common/nag-suppressions";
import { RuntimeIdentityName } from "../construct-groups/runtime-identities";
import { ConnectorOpsModule, ConnectorOpsProvider } from "./connector-ops-provider";
import { FrameworkValues } from "./framework-values";

/** The CodeBuild image the build runs in, by version tag: Amazon Linux 2023 on arm64. */
const IMAGE_BUILD_ENVIRONMENT = LinuxArmBuildImage.AMAZON_LINUX_2023_STANDARD_3_0;

/** The custom resource's attribute: the image URI, which exists once the resource is complete. */
export enum ConnectorImageAttribute {
  IMAGE_URI = "ImageUri",
}

/** A connector image a `CustomConnector` registers: in its stack, or already in a repository. */
export interface IConnectorImage extends IConstruct {
  readonly repository: IRepository;
  /** `<repository URI>:<tag>`, by tag, never by digest. */
  readonly imageUri: string;
}

/** An image another stack built: its repository and tag. */
export interface ConnectorImageAttributes {
  readonly repositoryName: string;
  readonly imageTag: string;
}

export interface ConnectorImageProps {
  readonly framework: FrameworkValues;
  /** Must start with `ccf-connectors/`, the only repositories the framework's Batch execution role pulls from. */
  readonly repositoryName: string;
  /** The Docker build context directory. */
  readonly directory: string;
  /**
   * The Dockerfile path, relative to `directory`. A `<file>.dockerignore` beside it, or else
   * `directory/.dockerignore`, decides what the source asset holds, as it does for `docker build -f`.
   */
  readonly file: string;
  /** For the repository. */
  readonly removalPolicy: RemovalPolicy;
  /**
   * How long after its push every image expires, including the one the custom connectors run. A
   * redeploy with changed source pushes a new image; a redeploy without changes pushes nothing, so
   * an image whose source hasn't changed for this long expires and every job then fails to pull it.
   */
  readonly maxImageAge: Duration;
  /** For the build's and the provider's log groups. */
  readonly logRetention: RetentionDays;
  /** How long one build may run, and how long the provider waits for it. */
  readonly buildTimeout: Duration;
}

/** The problem with a repository name outside `ccf-connectors/`, or undefined. */
export const repositoryNameProblem = (repositoryName: string): string | undefined =>
  repositoryName.startsWith(RuntimeIdentityName.CONNECTOR_REPOSITORY_PREFIX)
    ? undefined
    : `repositoryName "${repositoryName}" must start with "${RuntimeIdentityName.CONNECTOR_REPOSITORY_PREFIX}", ` +
      "because the framework's Batch execution role pulls only from those repositories.";

const checkRepositoryName = (repositoryName: string): void => {
  const problem = repositoryNameProblem(repositoryName);
  if (problem !== undefined) throw new Error(problem);
};

/** A Docker build context and its Dockerfile. */
interface BuildContext {
  readonly directory: string;
  /** The Dockerfile path, relative to `directory`. */
  readonly file: string;
}

/** The ignore file `docker build -f <file>` reads: `<file>.dockerignore`, else the context's `.dockerignore`. */
const ignoreFileOf = ({ directory, file }: BuildContext): string | undefined =>
  [path.join(directory, `${file}.dockerignore`), path.join(directory, ".dockerignore")].find(
    (candidate) => existsSync(candidate),
  );

/** The ignore file's patterns, keeping the Dockerfile and the ignore file itself in the asset. */
const sourceExcludes = (context: BuildContext): string[] => {
  const ignoreFile = ignoreFileOf(context);
  if (ignoreFile === undefined) return [];
  const patterns = readFileSync(ignoreFile, "utf8")
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line.length > 0 && !line.startsWith("#"));
  return [...patterns, `!${path.relative(context.directory, ignoreFile)}`, `!${context.file}`];
};

/** An image another stack built, in its repository; it creates nothing. */
class ImportedConnectorImage extends Construct implements IConnectorImage {
  public readonly repository: IRepository;
  public readonly imageUri: string;

  constructor(scope: Construct, id: string, attributes: ConnectorImageAttributes) {
    const { repositoryName, imageTag } = attributes;
    super(scope, id);
    checkRepositoryName(repositoryName);
    this.repository = Repository.fromRepositoryName(this, "Repository", repositoryName);
    this.imageUri = this.repository.repositoryUriForTag(imageTag);
  }
}

/**
 * The tenant's connector image: its ECR repository, which only the framework's Batch execution
 * role may pull from, and the image, built in this stack. CDK uploads the Docker build context as
 * a file asset; an arm64 CodeBuild project builds the image from it and pushes it into the
 * repository, tagged with the asset's hash, a content tag that changes with the source. A custom
 * resource starts the build and waits for it, so `imageUri` names an image that exists before any
 * `CustomConnector` that depends on this construct registers it. When the repository already holds
 * the tag, as on a rollback to an earlier source, the resource starts no build, because the
 * repository's tags are immutable and a second push of a tag fails.
 */
export class ConnectorImage extends Construct implements IConnectorImage {
  public readonly repository: Repository;
  public readonly imageUri: string;
  public readonly imageTag: string;
  public readonly project: Project;

  /** An image already in a repository, by its tag; creates nothing. */
  public static fromRepositoryName(
    scope: Construct,
    id: string,
    attributes: ConnectorImageAttributes,
  ): IConnectorImage {
    return new ImportedConnectorImage(scope, id, attributes);
  }

  constructor(scope: Construct, id: string, props: ConnectorImageProps) {
    const {
      framework,
      repositoryName,
      directory,
      file,
      removalPolicy,
      maxImageAge,
      logRetention,
      buildTimeout,
    } = props;
    super(scope, id);
    checkRepositoryName(repositoryName);
    this.repository = new Repository(this, "Repository", {
      repositoryName,
      imageScanOnPush: true,
      imageTagMutability: TagMutability.IMMUTABLE,
      removalPolicy,
      emptyOnDelete: removalPolicy === RemovalPolicy.DESTROY,
      lifecycleRules: [
        {
          description:
            "Expire every image, including the one in use, at its maximum age after its push; redeploy with changed source first.",
          tagStatus: TagStatus.ANY,
          maxImageAge,
        },
      ],
    });
    this.repository.grantPull(new ArnPrincipal(framework.batchExecutionRoleArn));

    const source = new Asset(this, "Source", {
      path: directory,
      ignoreMode: IgnoreMode.DOCKER,
      exclude: sourceExcludes({ directory, file }),
    });
    this.imageTag = source.assetHash;
    this.imageUri = this.repository.repositoryUriForTag(this.imageTag);

    const logGroup = new LogGroup(this, "BuildLogs", {
      retention: logRetention,
      removalPolicy: RemovalPolicy.DESTROY,
    });
    const role = new Role(this, "BuildRole", {
      assumedBy: new ServicePrincipal("codebuild.amazonaws.com"),
      description: `Builds the connector image and pushes it to ${repositoryName}.`,
    });
    this.repository.grantPush(role);
    logGroup.grantWrite(role);
    // Asset.grantRead would add s3:List* and s3:GetBucket* on the whole bucket.
    role.addToPolicy(
      new PolicyStatement({
        sid: "ReadImageSource",
        actions: ["s3:GetObject", "s3:GetObjectVersion"],
        resources: [`arn:${Aws.PARTITION}:s3:::${source.s3BucketName}/${source.s3ObjectKey}`],
      }),
    );
    this.project = new Project(this, "Build", {
      description: `Builds the connector image in ${repositoryName} from its source asset.`,
      role: role.withoutPolicyUpdates(),
      source: Source.s3({ bucket: source.bucket, path: source.s3ObjectKey }),
      environment: {
        buildImage: IMAGE_BUILD_ENVIRONMENT,
        computeType: ComputeType.SMALL,
        // `docker build` needs the Docker daemon, which runs only in privileged mode.
        privileged: true,
      },
      environmentVariables: {
        REGISTRY: { value: `${Aws.ACCOUNT_ID}.dkr.ecr.${Aws.REGION}.${Aws.URL_SUFFIX}` },
        IMAGE_URI: { value: this.imageUri },
        DOCKERFILE: { value: file },
      },
      buildSpec: BuildSpec.fromObject({
        version: "0.2",
        phases: {
          pre_build: {
            commands: [
              'aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$REGISTRY"',
            ],
          },
          build: {
            commands: ['docker build --platform linux/arm64 -f "$DOCKERFILE" -t "$IMAGE_URI" .'],
          },
          post_build: { commands: ['docker push "$IMAGE_URI"'] },
        },
      }),
      logging: { cloudWatch: { logGroup } },
      timeout: buildTimeout,
    });
    this.project.node.addDependency(role);

    const provider = ConnectorOpsProvider.of({
      scope: this,
      props: {
        module: ConnectorOpsModule.IMAGE_BUILD,
        environment: {},
        logRetention,
        totalTimeout: buildTimeout,
      },
    });
    // Start and read builds of this project only, and check whether a tag is already in its repository.
    provider.onEvent.addToRolePolicy(
      new PolicyStatement({
        actions: ["codebuild:StartBuild"],
        resources: [this.project.projectArn],
      }),
    );
    provider.onEvent.addToRolePolicy(
      new PolicyStatement({
        actions: ["ecr:DescribeImages"],
        resources: [this.repository.repositoryArn],
      }),
    );
    provider.isComplete.addToRolePolicy(
      new PolicyStatement({
        actions: ["codebuild:BatchGetBuilds"],
        resources: [this.project.projectArn],
      }),
    );
    const build = new CustomResource(this, "Image", {
      serviceToken: provider.serviceToken,
      resourceType: "Custom::CcfConnectorImage",
      properties: {
        ProjectName: this.project.projectName,
        RepositoryName: this.repository.repositoryName,
        ImageTag: this.imageTag,
        ImageUri: this.imageUri,
      },
    });
    build.node.addDependency(this.repository);
    acknowledgeNagRules({
      scopes: [this.project, role],
      acknowledgments: [
        ...CONNECTOR_IMAGE_ACKNOWLEDGMENTS,
        {
          id: "AwsSolutions-IAM5",
          reason: CONNECTOR_IMAGE_WILDCARD_REASON,
          findings: [
            "Resource::*",
            ...resourceFindings({ scope: this, resources: [`${logGroup.logGroupArn}:*`] }),
          ],
        },
      ],
    });
  }
}
