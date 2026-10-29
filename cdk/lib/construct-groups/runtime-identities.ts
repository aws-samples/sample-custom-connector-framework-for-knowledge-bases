import { Stack } from "aws-cdk-lib";
import {
  Effect,
  ManagedPolicy,
  PolicyDocument,
  PolicyStatement,
  Role,
  ServicePrincipal,
} from "aws-cdk-lib/aws-iam";
import { IKey } from "aws-cdk-lib/aws-kms";
import { IBucket } from "aws-cdk-lib/aws-s3";
import { Construct } from "constructs";
import { createClientImageAssetsRepositoryName } from "../common/client-image-assets-repository";
import {
  ProjectName,
  ResourceName,
  createConstructId,
  createResourceName,
  resolveAllowedClientAccountIds,
} from "../common/config";

/** Path and name prefix of the service-managed per-connector runtime roles. */
export const RUNTIME_ROLE_PATH = "/ccf-runtime/";
export const RUNTIME_ROLE_NAME_PREFIX = "CcfConnectorRuntime-";

export interface RuntimeIdentitiesProps {
  readonly projectName: ProjectName;
  readonly ingestionBucket: IBucket;
  readonly serviceKey: IKey;
  readonly allowedClientAccountIds: readonly string[];
}

/**
 * The two framework-account identities a connector run uses.
 *
 * `executionPrincipal` is the AWS Batch execution role: it pulls the registered
 * client image and publishes job logs. Connector code never receives its
 * credentials.
 *
 * Each connector additionally gets one service-managed runtime role created by
 * the control plane under {@link RUNTIME_ROLE_PATH}. That role carries the task
 * credentials the container sees, and its inline policy names only that
 * connector's API resources, destination prefix, and exact source role.
 * `runtimeRoleBoundary` caps what any such role can ever be granted.
 */
export class RuntimeIdentities extends Construct {
  public readonly executionPrincipal: Role;
  public readonly runtimeRoleBoundary: ManagedPolicy;
  public readonly runtimeRoleArnPattern: string;

  constructor(scope: Construct, id: string, props: RuntimeIdentitiesProps) {
    super(scope, id);
    const { projectName, ingestionBucket, serviceKey } = props;
    const { partition, region, account } = Stack.of(this);
    const clientAccountIds = resolveAllowedClientAccountIds({
      values: props.allowedClientAccountIds,
      frameworkAccount: account,
    });
    this.runtimeRoleArnPattern = `arn:${partition}:iam::${account}:role${RUNTIME_ROLE_PATH}${RUNTIME_ROLE_NAME_PREFIX}*`;
    this.executionPrincipal = new Role(
      this,
      createConstructId(ResourceName.FRAMEWORK_EXECUTION_ROLE),
      {
        roleName: createResourceName(projectName, ResourceName.FRAMEWORK_EXECUTION_ROLE),
        assumedBy: new ServicePrincipal("ecs-tasks.amazonaws.com"),
        description:
          "Batch execution principal that pulls connector images and writes job logs. Never passed to connector code.",
      },
    );
    this.executionPrincipal.addToPolicy(
      new PolicyStatement({
        effect: Effect.ALLOW,
        actions: ["ecr:GetAuthorizationToken"],
        // ECR does not support resource-level permissions for GetAuthorizationToken.
        resources: ["*"],
      }),
    );
    if (clientAccountIds.length > 0) {
      this.executionPrincipal.addToPolicy(
        new PolicyStatement({
          effect: Effect.ALLOW,
          actions: [
            "ecr:BatchCheckLayerAvailability",
            "ecr:GetDownloadUrlForLayer",
            "ecr:BatchGetImage",
          ],
          resources: clientAccountIds.map(
            (clientAccountId) =>
              `arn:${partition}:ecr:${region}:${clientAccountId}:repository/${createClientImageAssetsRepositoryName({ account: clientAccountId, region })}`,
          ),
        }),
      );
    }
    this.executionPrincipal.addToPolicy(
      new PolicyStatement({
        effect: Effect.ALLOW,
        actions: ["logs:CreateLogStream", "logs:PutLogEvents"],
        resources: [
          `arn:${partition}:logs:${region}:${account}:log-group:/aws/batch/job:log-stream:*`,
        ],
      }),
    );
    this.runtimeRoleBoundary = new ManagedPolicy(this, createConstructId("RuntimeRoleBoundary"), {
      managedPolicyName: createResourceName(projectName, "ConnectorRuntimeBoundary"),
      description:
        "Caps every service-managed connector runtime role to one connector's API, destination prefix, and source role.",
      document: this.createBoundaryDocument({
        partition,
        region,
        account,
        clientAccountIds,
        ingestionBucketArn: ingestionBucket.bucketArn,
        serviceKeyArn: serviceKey.keyArn,
      }),
    });
  }

  private createBoundaryDocument(request: {
    readonly partition: string;
    readonly region: string;
    readonly account: string;
    readonly clientAccountIds: readonly string[];
    readonly ingestionBucketArn: string;
    readonly serviceKeyArn: string;
  }): PolicyDocument {
    const { partition, region, account, clientAccountIds, ingestionBucketArn, serviceKeyArn } =
      request;
    const statements = [
      new PolicyStatement({
        effect: Effect.ALLOW,
        actions: ["execute-api:Invoke"],
        // The REST API id is unresolved while the boundary is created. The
        // per-connector inline policy names the exact API, stage, and routes.
        resources: [
          `arn:${partition}:execute-api:${region}:${account}:*/*/*/api/v1/custom-connectors/*`,
        ],
      }),
      new PolicyStatement({
        effect: Effect.ALLOW,
        actions: ["s3:PutObject", "s3:DeleteObject"],
        resources: [`${ingestionBucketArn}/documents/*`],
      }),
      new PolicyStatement({
        effect: Effect.ALLOW,
        actions: ["kms:GenerateDataKey", "kms:Decrypt"],
        resources: [serviceKeyArn],
        conditions: { StringEquals: { "kms:ViaService": `s3.${region}.amazonaws.com` } },
      }),
    ];
    if (clientAccountIds.length > 0) {
      statements.push(
        new PolicyStatement({
          effect: Effect.ALLOW,
          actions: ["sts:AssumeRole"],
          resources: clientAccountIds.map(
            (clientAccountId) =>
              `arn:${partition}:iam::${clientAccountId}:role/${createResourceName(ProjectName.CCF, ResourceName.SOURCE_ROLE_PREFIX)}-*`,
          ),
        }),
      );
    }
    return new PolicyDocument({ statements });
  }
}
