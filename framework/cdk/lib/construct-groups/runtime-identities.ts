import { Aws } from "aws-cdk-lib";
import {
  Effect,
  ManagedPolicy,
  PolicyStatement,
  Role,
  ServicePrincipal,
} from "aws-cdk-lib/aws-iam";
import { Construct } from "constructs";
import { LOG_ROUTER_LOGS, ROLE_PATH } from "../common/config";
import { runtimeRoleTagsContract } from "../common/contracts";
import {
  acknowledgeNagRules,
  actionsWithoutResourceAcknowledgment,
  resourceFindings,
} from "../common/nag-suppressions";

/** The names of the per-custom connector runtime roles the backend creates and the deletion workflow deletes. */
export enum RuntimeRoleName {
  PATH = "/ccf-runtime/",
  NAME_PREFIX = "CcfConnectorRuntime-",
  POLICY = "CcfConnectorRuntimePolicy",
}

/** The reason for the API, which creates and passes runtime roles. */
export const PASS_RUNTIME_ROLES_REASON =
  "Runtime roles are created per custom connector at run time, so the roles that pass them name " +
  `role${RuntimeRoleName.PATH}${RuntimeRoleName.NAME_PREFIX}*; the API's CreateRole requires the ` +
  "CcfConnectorRuntimeBoundary boundary.";

/** The fixed names other accounts' repository policies name. */
export enum RuntimeIdentityName {
  BATCH_EXECUTION_ROLE = "CcfBatchExecutionRole",
  RUNTIME_BOUNDARY = "CcfConnectorRuntimeBoundary",
  /** Every connector image repository's name starts with this. */
  CONNECTOR_REPOSITORY_PREFIX = "ccf-connectors/",
}

export interface RuntimeIdentitiesProps {
  /** The service account and every allowed tenant account, whose connector repositories jobs pull from. */
  readonly imageAccountIds: readonly string[];
}

/**
 * The service-account identities a job uses.
 *
 * `batchExecutionRole` is the role AWS Batch and Fargate use before the containers start: it
 * pulls the connector image from a tenant's `ccf-connectors/` repository and writes the log
 * router's own diagnostics. Its credentials never reach the containers.
 *
 * `runtimeRoleBoundary` caps every runtime role the backend creates: `execute-api:Invoke` on
 * the role's own destination and custom connector routes, and `sts:AssumeRole` on its own
 * tenant roles, both through the role's tags, and nothing else.
 */
export class RuntimeIdentities extends Construct {
  public readonly batchExecutionRole: Role;
  public readonly runtimeRoleBoundary: ManagedPolicy;
  /** Every runtime role's ARN, for `iam:CreateRole` and `iam:PassRole` grants. */
  public readonly runtimeRoleArnPattern: string;

  constructor(scope: Construct, id: string, props: RuntimeIdentitiesProps) {
    super(scope, id);
    this.runtimeRoleArnPattern = `arn:${Aws.PARTITION}:iam::${Aws.ACCOUNT_ID}:role${RuntimeRoleName.PATH}${RuntimeRoleName.NAME_PREFIX}*`;
    this.batchExecutionRole = new Role(this, "BatchExecutionRole", {
      roleName: RuntimeIdentityName.BATCH_EXECUTION_ROLE,
      path: ROLE_PATH,
      assumedBy: new ServicePrincipal("ecs-tasks.amazonaws.com"),
      description:
        "Pulls connector images and writes the log router's diagnostics before a job's containers start.",
    });
    const executionStatements = batchExecutionStatements(props);
    for (const statement of executionStatements) this.batchExecutionRole.addToPolicy(statement);
    acknowledgeNagRules({
      scopes: [this.batchExecutionRole],
      acknowledgments: [
        actionsWithoutResourceAcknowledgment(["ecr:GetAuthorizationToken"]),
        {
          id: "AwsSolutions-IAM5",
          reason:
            "The role pulls only repositories under ccf-connectors/ in the service account and each allowed " +
            "tenant account, whose names the tenant chooses, and writes only the log router's own streams.",
          findings: resourceFindings({
            scope: this,
            resources: executionStatements
              .flatMap((statement) => statement.resources)
              .filter((resource) => resource !== "*"),
          }),
        },
      ],
    });
    const boundaryStatements = runtimeRoleBoundaryStatements();
    this.runtimeRoleBoundary = new ManagedPolicy(this, "RuntimeRoleBoundary", {
      managedPolicyName: RuntimeIdentityName.RUNTIME_BOUNDARY,
      description:
        "Caps every connector runtime role to its own API routes and its own tenant roles.",
      statements: boundaryStatements,
    });
    acknowledgeNagRules({
      scopes: [this.runtimeRoleBoundary],
      acknowledgments: [
        {
          id: "AwsSolutions-IAM5",
          reason:
            "The boundary caps every runtime role at once, so it names routes and tenant roles through the " +
            "role's own tags: the * segments of the execute-api ARNs are the API ID, stage, and method, and " +
            "the account wildcard of the sts:AssumeRole resources is fixed to the role's tenant account by " +
            "the aws:ResourceAccount condition; IAM accepts no policy variable in an IAM ARN's account. Each " +
            "runtime role's inline policy names exact routes and roles.",
          findings: resourceFindings({
            scope: this,
            resources: boundaryStatements
              .flatMap((statement) => statement.resources)
              .filter((resource) => resource.includes("*")),
          }),
        },
      ],
    });
  }
}

/** The three `CcfBatchExecutionRole` statements. */
const batchExecutionStatements = (props: RuntimeIdentitiesProps): PolicyStatement[] => [
  new PolicyStatement({
    sid: "EcrAuthorizationToken",
    effect: Effect.ALLOW,
    actions: ["ecr:GetAuthorizationToken"],
    resources: ["*"],
  }),
  new PolicyStatement({
    sid: "ConnectorImagePull",
    effect: Effect.ALLOW,
    actions: ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"],
    resources: [...new Set(props.imageAccountIds)].map(
      (account) =>
        `arn:${Aws.PARTITION}:ecr:${Aws.REGION}:${account}:repository/${RuntimeIdentityName.CONNECTOR_REPOSITORY_PREFIX}*`,
    ),
  }),
  new PolicyStatement({
    sid: "LogRouterDiagnostics",
    effect: Effect.ALLOW,
    actions: ["logs:CreateLogStream", "logs:PutLogEvents"],
    resources: [
      `arn:${Aws.PARTITION}:logs:${Aws.REGION}:${Aws.ACCOUNT_ID}:log-group:${LOG_ROUTER_LOGS.logGroup}:log-stream:${LOG_ROUTER_LOGS.streamPrefix}/*`,
    ],
  }),
];

/**
 * The `CcfConnectorRuntimeBoundary`. The policy variables sit only in the resource part
 * of each ARN; the tenant account is fixed by the `aws:ResourceAccount` condition, and a role
 * without a log-delivery tag leaves the second role's variable without a value, which matches no role.
 */
export const runtimeRoleBoundaryStatements = (): PolicyStatement[] => {
  const tags = runtimeRoleTagsContract();
  const tag = (key: string): string => `\${aws:PrincipalTag/${key}}`;
  const api = `arn:${Aws.PARTITION}:execute-api:${Aws.REGION}:${Aws.ACCOUNT_ID}:*/*`;
  const destination = `api/v1/destinations/${tag(tags.destinationId)}`;
  const connector = `${destination}/custom-connectors/${tag(tags.connectorId)}`;
  return [
    new PolicyStatement({
      sid: "OwnConnectorRoutes",
      effect: Effect.ALLOW,
      actions: ["execute-api:Invoke"],
      resources: [
        `${api}/GET/${destination}`,
        `${api}/GET/${connector}`,
        `${api}/*/${connector}/*`,
      ],
    }),
    new PolicyStatement({
      sid: "OwnTenantRoles",
      effect: Effect.ALLOW,
      actions: ["sts:AssumeRole"],
      resources: [
        `arn:${Aws.PARTITION}:iam::*:role/${tag(tags.sourceAccessRole)}`,
        `arn:${Aws.PARTITION}:iam::*:role/${tag(tags.logDeliveryRole)}`,
      ],
      conditions: {
        StringEquals: { "aws:ResourceAccount": tag(tags.tenantAccount) },
      },
    }),
  ];
};
