import { createHash } from "node:crypto";
import { Aws, CustomResource, Duration, RemovalPolicy, Stack } from "aws-cdk-lib";
import {
  IGrantable,
  IPrincipal,
  ManagedPolicy,
  PolicyDocument,
  PolicyStatement,
  Role,
} from "aws-cdk-lib/aws-iam";
import { LogGroup, RetentionDays } from "aws-cdk-lib/aws-logs";
import { ScheduleExpression } from "aws-cdk-lib/aws-scheduler";
import { Construct } from "constructs";
import { ROLE_PATH } from "../common/config";
import { acknowledgeNagRules } from "../common/nag-suppressions";
import {
  enumMember,
  enumValues,
  numberRange,
  operationRoutes,
  stringPattern,
} from "../common/contracts";
import {
  ConnectorOpsModule,
  ConnectorOpsProvider,
  frameworkApiEnvironment,
} from "./connector-ops-provider";
import { IConnectorImage } from "./connector-image";
import { IDestination } from "./destination";
import { FrameworkValues } from "./framework-values";
import { TenantAccessPolicy, TenantAccessRole } from "./tenant-access-role";

const MAX_ROLE_NAME_LENGTH = 64;
const NAME_HASH_LENGTH = 8;
const LOG_GROUP_PREFIX = "/ccf/connectors/";
const BOUNDARY_NAME_PREFIX = "CcfConnectorBoundary-";
/** The operations the registration's handlers call; no list operation. */
const REGISTRATION_OPERATIONS = [
  "CreateCustomConnector",
  "GetCustomConnector",
  "UpdateCustomConnector",
  "DeleteCustomConnector",
];
/** Fargate's memory values, in MiB, for each vCPU value: `minimum` to `maximum` in steps of `step`. */
const FARGATE_MEMORY_MIB = new Map<number, { minimum: number; maximum: number; step: number }>([
  [0.5, { minimum: 1024, maximum: 4096, step: 1024 }],
  [1, { minimum: 2048, maximum: 8192, step: 1024 }],
  [2, { minimum: 4096, maximum: 16384, step: 1024 }],
  [4, { minimum: 8192, maximum: 30720, step: 1024 }],
  [8, { minimum: 16384, maximum: 61440, step: 4096 }],
  [16, { minimum: 32768, maximum: 122880, step: 8192 }],
]);

/**
 * The services every custom connector's roles may call: the destination bucket (`s3`), the
 * source secret and its key (`secretsmanager`, `kms`), and the log group (`logs`).
 */
export const CONNECTOR_BOUNDARY_SERVICES = ["s3", "secretsmanager", "kms", "logs"] as const;
/** Services `sourceAccessServices` never accepts, because they would let a connector grant itself more. */
export const FORBIDDEN_SOURCE_ACCESS_SERVICES: readonly string[] = ["iam", "sts", "organizations"];
/** An IAM service prefix, such as `dynamodb` or `execute-api`. */
const SERVICE_PREFIX_PATTERN = /^[a-z][a-z0-9-]{0,63}$/;

/** The role name prefixes the framework accepts for a custom connector's tenant roles. */
export enum TenantRolePrefix {
  SOURCE_ACCESS = "CcfSourceRole-",
  LOG_DELIVERY = "CcfLogDeliveryRole-",
}

/** The custom resource's attributes, which `register.py` reads from the API's response. */
export enum RegistrationAttribute {
  CONNECTOR_ID = "ConnectorId",
  CONNECTOR_ARN = "ConnectorArn",
  RUNTIME_ROLE_ARN = "RuntimeRoleArn",
  EXTERNAL_ID = "ExternalId",
}

/** The job's task size and how long it may run. */
export interface CustomConnectorCompute {
  readonly cpu: number;
  readonly memoryMib: number;
  readonly timeout: Duration;
}

/** Turns on log delivery: the log group `/ccf/connectors/<name>` and its log-delivery role. */
export interface LogDeliveryProps {
  readonly retention: RetentionDays;
  readonly removalPolicy: RemovalPolicy;
}

export interface CustomConnectorProps {
  readonly framework: FrameworkValues;
  readonly destination: IDestination;
  readonly image: IConnectorImage;
  /** Unique among the account's custom connectors: the role and log group names come from it. */
  readonly name: string;
  readonly command: readonly string[];
  readonly compute: CustomConnectorCompute;
  readonly schedule?: ScheduleExpression;
  readonly logDelivery?: LogDeliveryProps;
  /** The implementation's configuration string, passed to the job exactly as given. */
  readonly connectorConfiguration?: string;
  /**
   * A secret the job gets as `JobContext.secret`. Grant the connector read access with
   * `secret.grantRead(connector)`; the registration only names it.
   */
  readonly secretArn?: string;
  /** For the registration provider's log groups. */
  readonly logRetention: RetentionDays;
  /** How long a delete may wait for the framework's connector deletion workflow. */
  readonly deletionTimeout: Duration;
  /**
   * Service prefixes, such as `dynamodb`, the source-access role may call besides
   * {@link CONNECTOR_BOUNDARY_SERVICES}, for an implementation that reads other resources in its
   * account through `source_session`; `[]` for none.
   */
  readonly sourceAccessServices: readonly string[];
}

/**
 * The first problem with a `sourceAccessServices` list: an entry that isn't a lowercase IAM
 * service prefix, or one of {@link FORBIDDEN_SOURCE_ACCESS_SERVICES}. Undefined when every entry is allowed.
 */
export const sourceAccessServicesProblem = (
  sourceAccessServices: readonly string[],
): string | undefined => {
  for (const [index, service] of sourceAccessServices.entries()) {
    const field = `sourceAccessServices[${index}]`;
    if (!SERVICE_PREFIX_PATTERN.test(service))
      return `${field} "${service}" must be a lowercase IAM service prefix, such as dynamodb.`;
    if (FORBIDDEN_SOURCE_ACCESS_SERVICES.includes(service))
      return `${field} "${service}" is not allowed: a connector can't call ${service}.`;
  }
  return undefined;
};

/** The first problem with a compute: a value outside the API's range, or a pair Fargate doesn't run. */
export const computeProblem = ({
  cpu,
  memoryMib,
  timeout,
}: CustomConnectorCompute): string | undefined => {
  const checks = [
    {
      field: "compute.cpu",
      unit: "vCPU",
      value: cpu,
      schema: "ResourceRequirements",
      member: "cpu",
    },
    {
      field: "compute.memoryMib",
      unit: "MiB",
      value: memoryMib,
      schema: "ResourceRequirements",
      member: "memory",
    },
    {
      field: "compute.timeout",
      unit: "seconds",
      value: timeout.toSeconds(),
      schema: "ContainerProperties",
      member: "timeout",
    },
  ];
  for (const { field, unit, value, schema, member } of checks) {
    const { minimum, maximum } = numberRange({ schema, member });
    if (value < minimum || value > maximum)
      return `${field} ${value} must be from ${minimum} to ${maximum} ${unit}.`;
  }
  const memory = FARGATE_MEMORY_MIB.get(cpu);
  const runs =
    memory !== undefined &&
    memoryMib >= memory.minimum &&
    memoryMib <= memory.maximum &&
    (memoryMib - memory.minimum) % memory.step === 0;
  return runs
    ? undefined
    : `Fargate doesn't run compute.cpu ${cpu} with compute.memoryMib ${memoryMib}.`;
};

interface ConnectorProblemRequest {
  readonly name: string;
  readonly compute: CustomConnectorCompute;
  readonly sourceAccessServices: readonly string[];
}

const connectorProblem = ({
  name,
  compute,
  sourceAccessServices,
}: ConnectorProblemRequest): string | undefined =>
  (stringPattern("ResourceName").test(name)
    ? undefined
    : `name "${name}" must match the API's ResourceName pattern, ${stringPattern("ResourceName").source}.`) ??
  computeProblem(compute) ??
  sourceAccessServicesProblem(sourceAccessServices);

/** The one log destination type the API takes; synthesis fails when the model adds another. */
const logDestinationType = (): string => {
  const values = enumValues("LogDestinationType");
  if (values.length !== 1)
    throw new Error(
      `CustomConnector sends the one LogDestinationType, and the model has ${values.length}.`,
    );
  return values[0];
};

interface RoleNameRequest {
  readonly prefix: TenantRolePrefix;
  readonly connectorName: string;
}

/** The prefix and the name, or as much of the name as fits with a hyphen and 8 hex of its SHA-256. */
const tenantRoleName = ({ prefix, connectorName }: RoleNameRequest): string => {
  const full = `${prefix}${connectorName}`;
  if (full.length <= MAX_ROLE_NAME_LENGTH) return full;
  const hash = createHash("sha256").update(connectorName).digest("hex").slice(0, NAME_HASH_LENGTH);
  const kept = connectorName.slice(0, MAX_ROLE_NAME_LENGTH - prefix.length - NAME_HASH_LENGTH - 1);
  return `${prefix}${kept}-${hash}`;
};

interface BoundaryRequest {
  readonly connectorName: string;
  readonly sourceAccessServices: readonly string[];
}

/**
 * One custom connector: the custom resource that registers it with `CreateCustomConnector`, its
 * own source-access role, and the log group and log-delivery role when `logDelivery` is given.
 * The schedule, when given, is part of the registration. Both roles trust only the custom
 * connector's runtime role, with its external ID.
 *
 * The connector is the grant principal of its source-access role, so `secret.grantRead(connector)`
 * or `bucket.grantRead(connector)` gives the job access to its source.
 */
export class CustomConnector extends Construct implements IGrantable {
  /** The source-access role; add any grant the implementation needs to read its source. */
  public readonly sourceRole: Role;
  public readonly grantPrincipal: IPrincipal;
  public readonly connectorId: string;
  public readonly connectorArn: string;

  constructor(scope: Construct, id: string, props: CustomConnectorProps) {
    const {
      framework,
      destination,
      image,
      name,
      command,
      compute,
      schedule,
      logDelivery,
      connectorConfiguration,
      secretArn,
      logRetention,
      deletionTimeout,
      sourceAccessServices,
    } = props;
    super(scope, id);
    const problem = connectorProblem({ name, compute, sourceAccessServices });
    if (problem !== undefined) throw new Error(problem);
    const stack = Stack.of(this);
    const sourceRoleName = tenantRoleName({
      prefix: TenantRolePrefix.SOURCE_ACCESS,
      connectorName: name,
    });
    const logDeliveryRoleName = tenantRoleName({
      prefix: TenantRolePrefix.LOG_DELIVERY,
      connectorName: name,
    });
    const roleArn = (roleName: string): string =>
      `arn:${Aws.PARTITION}:iam::${stack.account}:role/${roleName}`;
    const logGroupName = `${LOG_GROUP_PREFIX}${name}`;
    const logGroupArn = `arn:${Aws.PARTITION}:logs:${stack.region}:${stack.account}:log-group:${logGroupName}`;
    if (logDelivery !== undefined)
      new LogGroup(this, "Logs", {
        logGroupName,
        retention: logDelivery.retention,
        removalPolicy: logDelivery.removalPolicy,
      });

    const api = framework.frameworkApi();
    const provider = ConnectorOpsProvider.of({
      scope: this,
      props: {
        module: ConnectorOpsModule.REGISTER,
        environment: frameworkApiEnvironment(api),
        logRetention,
        totalTimeout: deletionTimeout,
      },
    });
    provider.allowRoutes({ routes: operationRoutes(REGISTRATION_OPERATIONS), api });
    const registration = new CustomResource(this, "Registration", {
      serviceToken: provider.serviceToken,
      resourceType: "Custom::CcfCustomConnector",
      properties: {
        // JSON keeps the numbers numbers; CloudFormation would send each property value as a string.
        Request: stack.toJsonString({
          destination_id: destination.destinationId,
          name,
          container_properties: {
            image_uri: image.imageUri,
            command,
            resource_requirements: { cpu: compute.cpu, memory: compute.memoryMib },
            timeout: compute.timeout.toSeconds(),
          },
          source_access: {
            role_arn: roleArn(sourceRoleName),
            ...(secretArn === undefined ? {} : { secret_arn: secretArn }),
          },
          ...(connectorConfiguration === undefined
            ? {}
            : { connector_configuration: connectorConfiguration }),
          ...(logDelivery === undefined
            ? {}
            : {
                log_destination: {
                  type: logDestinationType(),
                  arn: logGroupArn,
                  role_arn: roleArn(logDeliveryRoleName),
                },
              }),
          ...(schedule === undefined
            ? {}
            : {
                schedule: {
                  expression: schedule.expressionString,
                  state: enumMember({ schema: "ScheduleState", member: "ENABLED" }),
                  ...(schedule.timeZone === undefined
                    ? {}
                    : { timezone: schedule.timeZone.timezoneName }),
                },
              }),
        }),
      },
    });
    // The registration names the image by tag, so the image's build and push must finish first.
    registration.node.addDependency(image);
    this.connectorId = registration.getAttString(RegistrationAttribute.CONNECTOR_ID);
    this.connectorArn = registration.getAttString(RegistrationAttribute.CONNECTOR_ARN);

    const tenantRole = {
      registration,
      serviceAccount: framework.serviceAccount,
      boundary: this.createBoundary({ connectorName: name, sourceAccessServices }),
    };
    this.sourceRole = new TenantAccessRole(this, "SourceRole", {
      ...tenantRole,
      roleName: sourceRoleName,
      description: `Source access for the custom connector ${name}.`,
      policy: sourceAccessPolicy({
        bucketName: destination.bucketName,
        connectorId: this.connectorId,
      }),
    }).role;
    this.grantPrincipal = this.sourceRole;
    if (logDelivery !== undefined) {
      const logStreams = `${logGroupArn}:*`;
      new TenantAccessRole(this, "LogDeliveryRole", {
        ...tenantRole,
        roleName: logDeliveryRoleName,
        description: `Log delivery for the custom connector ${name}.`,
        policy: {
          wildcardResources: [logStreams],
          document: new PolicyDocument({
            statements: [
              new PolicyStatement({
                sid: "WriteLogDestination",
                actions: ["logs:CreateLogStream", "logs:PutLogEvents"],
                resources: [logStreams],
              }),
            ],
          }),
        },
      });
    }
  }

  /**
   * The boundary of the custom connector's roles: {@link CONNECTOR_BOUNDARY_SERVICES} and each
   * `sourceAccessServices` entry, once. A grant added to the source-access role later, such as
   * `bucket.grantRead(connector)`, works only for those services.
   */
  private createBoundary({ connectorName, sourceAccessServices }: BoundaryRequest): ManagedPolicy {
    const services = [...new Set([...CONNECTOR_BOUNDARY_SERVICES, ...sourceAccessServices])];
    const policy = new ManagedPolicy(this, "Boundary", {
      managedPolicyName: `${BOUNDARY_NAME_PREFIX}${connectorName}`,
      path: ROLE_PATH,
      description: `Permissions boundary of the custom connector ${connectorName}'s roles.`,
      statements: [
        new PolicyStatement({
          sid: "ServicesTheConnectorCalls",
          actions: services.map((service) => `${service}:*`),
          resources: ["*"],
        }),
      ],
    });
    acknowledgeNagRules({
      scopes: [policy],
      acknowledgments: [
        {
          id: "AwsSolutions-IAM5",
          reason:
            "A permissions boundary caps the services a role can call; each role's own policy scopes the " +
            "actions and resources.",
          findings: [...services.map((service) => `Action::${service}:*`), "Resource::*"],
        },
      ],
    });
    return policy;
  }
}

interface SourceAccessPolicyRequest {
  readonly bucketName: string;
  readonly connectorId: string;
}

/** Objects and listing under `connector_id=<connector ID>/` in the destination bucket only. */
const sourceAccessPolicy = ({
  bucketName,
  connectorId,
}: SourceAccessPolicyRequest): TenantAccessPolicy => {
  const bucket = `arn:${Aws.PARTITION}:s3:::${bucketName}`;
  const prefix = `connector_id=${connectorId}/`;
  const objects = `${bucket}/${prefix}*`;
  return {
    wildcardResources: [objects],
    document: new PolicyDocument({
      statements: [
        new PolicyStatement({
          sid: "ConnectorPrefixObjects",
          actions: ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
          resources: [objects],
        }),
        new PolicyStatement({
          sid: "ConnectorPrefixList",
          actions: ["s3:ListBucket"],
          resources: [bucket],
          conditions: { StringLike: { "s3:prefix": `${prefix}*` } },
        }),
      ],
    }),
  };
};
