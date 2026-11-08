import { Aws, Duration, RemovalPolicy, Stack, TimeZone } from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { RetentionDays } from "aws-cdk-lib/aws-logs";
import { ScheduleExpression } from "aws-cdk-lib/aws-scheduler";
import { Secret } from "aws-cdk-lib/aws-secretsmanager";
import { isJsonObject } from "../lib/common/app-config";
import { stringPattern } from "../lib/common/contracts";
import { ConnectorImage, IConnectorImage } from "../lib/constructs/connector-image";
import {
  CustomConnector,
  CustomConnectorCompute,
  CustomConnectorProps,
  TenantRolePrefix,
  computeProblem,
} from "../lib/constructs/custom-connector";
import { Destination } from "../lib/constructs/destination";
import {
  BUCKET_NAME,
  DESTINATION_ID,
  IMAGE_DIRECTORY,
  JsonObject,
  SERVICE_ACCOUNT,
  TENANT_ACCOUNT,
  literalFramework,
  onlyResourceId,
  partitionArn,
  propertiesOf,
  resourcesOf,
  statementsOf,
  tenantStack,
} from "./construct-fixtures";
import { componentSchema, schemaProblems } from "./openapi-schema";
import { connectorKeyExample } from "./identifier-examples";

type ConnectorOptions = Partial<Omit<CustomConnectorProps, "framework" | "destination" | "image">>;

interface SynthesizeRequest {
  readonly connector?: ConnectorOptions;
  readonly builtImage?: boolean;
}

interface Synthesized {
  readonly stack: Stack;
  readonly connector: CustomConnector;
  readonly template: Template;
  readonly registrationId: string;
}

const SECRET_ARN = `arn:aws:secretsmanager:us-east-1:${TENANT_ACCOUNT}:secret:github-token-AbCdEf`;
const COMPUTE: CustomConnectorCompute = { cpu: 1, memoryMib: 2048, timeout: Duration.minutes(15) };
const LOG_DELIVERY = { retention: RetentionDays.ONE_MONTH, removalPolicy: RemovalPolicy.RETAIN };
/** The values each pseudo parameter takes in a us-east-1 deployment in the tenant account. */
const PSEUDO_PARAMETERS: Readonly<Record<string, string>> = {
  "AWS::Partition": "aws",
  "AWS::Region": "us-east-1",
  "AWS::URLSuffix": "amazonaws.com",
  "AWS::AccountId": TENANT_ACCOUNT,
};

const requiredProps = (stack: Stack): Omit<CustomConnectorProps, "image"> => ({
  framework: literalFramework(),
  destination: Destination.fromDestinationAttributes(stack, "Destination", {
    destinationId: DESTINATION_ID,
    bucketName: BUCKET_NAME,
  }),
  name: "github-awslabs",
  command: ["ccf-connector", "run", "github_connector:GitHubConnectorImplementation"],
  compute: COMPUTE,
  logRetention: RetentionDays.ONE_MONTH,
  deletionTimeout: Duration.hours(1),
  sourceAccessServices: [],
});

const importedImage = (stack: Stack): IConnectorImage =>
  ConnectorImage.fromRepositoryName(stack, "Image", {
    repositoryName: "ccf-connectors/github",
    imageTag: "1.0.0",
  });

const synthesize = ({
  connector = {},
  builtImage = false,
}: SynthesizeRequest = {}): Synthesized => {
  const stack = tenantStack("Connector");
  const props = requiredProps(stack);
  const image = builtImage
    ? new ConnectorImage(stack, "Image", {
        framework: props.framework,
        repositoryName: "ccf-connectors/github",
        directory: IMAGE_DIRECTORY,
        file: "Dockerfile",
        removalPolicy: RemovalPolicy.RETAIN,
        maxImageAge: Duration.days(365),
        logRetention: RetentionDays.ONE_MONTH,
        buildTimeout: Duration.hours(1),
      })
    : importedImage(stack);
  const created = new CustomConnector(stack, "Connector", {
    ...props,
    image,
    connectorConfiguration: '{"owner":"awslabs"}',
    secretArn: SECRET_ARN,
    ...connector,
  });
  const template = Template.fromStack(stack);
  return {
    stack,
    connector: created,
    template,
    registrationId: onlyResourceId(template, "Custom::CcfCustomConnector"),
  };
};

/** A synthesized string with each pseudo parameter at its us-east-1 value and each attribute as `<ID.Attribute>`. */
const concrete = (value: unknown): string => {
  if (typeof value === "string") return value;
  if (!isJsonObject(value))
    throw new Error(`${JSON.stringify(value)} isn't a string or an intrinsic.`);
  const join = value["Fn::Join"];
  if (Array.isArray(join) && Array.isArray(join[1]))
    return join[1].map((part: unknown) => concrete(part)).join(String(join[0]));
  const reference = value.Ref;
  if (typeof reference === "string" && Object.hasOwn(PSEUDO_PARAMETERS, reference))
    return PSEUDO_PARAMETERS[reference];
  const attribute = value["Fn::GetAtt"];
  if (Array.isArray(attribute)) return `<${attribute.join(".")}>`;
  throw new Error(`${JSON.stringify(value)} isn't an intrinsic this test resolves.`);
};

/** The registration's one property, parsed. */
const requestOf = (template: Template): JsonObject => {
  const properties = propertiesOf(resourcesOf(template, "Custom::CcfCustomConnector")[0]);
  const request: unknown = JSON.parse(concrete(properties.Request));
  if (!isJsonObject(request)) throw new Error("The registration's Request isn't a JSON object.");
  return { ...request };
};

const roleStartingWith = (template: Template, prefix: string): JsonObject[] =>
  resourcesOf(template, "AWS::IAM::Role").filter((role) =>
    String(propertiesOf(role).RoleName).startsWith(prefix),
  );
const attribute = (registrationId: string, name: string): unknown => ({
  "Fn::GetAtt": [registrationId, name],
});

const trustPolicy = (registrationId: string): unknown => ({
  Version: "2012-10-17",
  Statement: [
    {
      Effect: "Allow",
      Principal: {
        AWS: {
          "Fn::Join": ["", ["arn:", { Ref: "AWS::Partition" }, `:iam::${SERVICE_ACCOUNT}:root`]],
        },
      },
      Action: "sts:AssumeRole",
      Condition: {
        StringEquals: { "sts:ExternalId": attribute(registrationId, "ExternalId") },
        ArnEquals: { "aws:PrincipalArn": attribute(registrationId, "RuntimeRoleArn") },
      },
    },
  ],
});

describe("CustomConnector's source-access role", () => {
  const { stack, template, registrationId } = synthesize();
  const [role] = roleStartingWith(template, TenantRolePrefix.SOURCE_ACCESS);
  const bucket = `s3:::${BUCKET_NAME}`;

  it("trusts only the custom connector's runtime role, pinned by ARN, with its external ID", () => {
    expect(propertiesOf(role).AssumeRolePolicyDocument).toEqual(trustPolicy(registrationId));
    expect(role.DependsOn).toEqual(expect.arrayContaining([registrationId]));
  });

  it("reads and writes only the custom connector's own prefix", () => {
    const { keyStart, keyEnd } = connectorKeyExample();
    const objects = {
      "Fn::Join": [
        "",
        [
          "arn:",
          { Ref: "AWS::Partition" },
          `:${bucket}/${keyStart}`,
          attribute(registrationId, "ConnectorId"),
          `${keyEnd}*`,
        ],
      ],
    };

    expect(propertiesOf(role).Policies).toEqual([
      {
        PolicyName: "CcfConnectorAccess",
        PolicyDocument: {
          Version: "2012-10-17",
          Statement: [
            {
              Sid: "ConnectorPrefixObjects",
              Effect: "Allow",
              Action: ["s3:DeleteObject", "s3:GetObject", "s3:PutObject"],
              Resource: objects,
            },
            {
              Sid: "ConnectorPrefixList",
              Effect: "Allow",
              Action: "s3:ListBucket",
              Resource: partitionArn(stack, bucket),
              Condition: {
                StringLike: {
                  "s3:prefix": {
                    "Fn::Join": [
                      "",
                      [keyStart, attribute(registrationId, "ConnectorId"), `${keyEnd}*`],
                    ],
                  },
                },
              },
            },
          ],
        },
      },
    ]);
  });

  it("is registered by an ARN the API's SourceRoleArn pattern accepts", () => {
    const sourceAccess = requestOf(template).source_access;
    const roleArn = isJsonObject(sourceAccess) ? sourceAccess.role_arn : undefined;

    expect(roleArn).toBe(`arn:aws:iam::${TENANT_ACCOUNT}:role/CcfSourceRole-github-awslabs`);
    expect(stringPattern("SourceRoleArn").test(String(roleArn))).toBe(true);
  });
});

describe("CustomConnector as a grant principal", () => {
  it("gets read access to a secret through secret.grantRead(connector)", () => {
    const stack = tenantStack("Connector");
    const connector = new CustomConnector(stack, "Connector", {
      ...requiredProps(stack),
      image: importedImage(stack),
    });
    const secret = new Secret(stack, "Token");
    secret.grantRead(connector);
    const template = Template.fromStack(stack);
    const [policy] = resourcesOf(template, "AWS::IAM::Policy").filter((candidate) =>
      JSON.stringify(propertiesOf(candidate).PolicyDocument).includes(
        "secretsmanager:GetSecretValue",
      ),
    );
    const [sourceRoleId] = Object.entries(template.findResources("AWS::IAM::Role"))
      .filter(([, role]) =>
        String(propertiesOf(role).RoleName).startsWith(TenantRolePrefix.SOURCE_ACCESS),
      )
      .map(([logicalId]) => logicalId);

    expect(propertiesOf(policy).Roles).toEqual([{ Ref: sourceRoleId }]);
    expect(statementsOf(propertiesOf(policy).PolicyDocument)).toEqual([
      expect.objectContaining({
        Action: ["secretsmanager:DescribeSecret", "secretsmanager:GetSecretValue"],
        Resource: { Ref: onlyResourceId(template, "AWS::SecretsManager::Secret") },
      }),
    ]);
  });
});

describe("CustomConnector's registration", () => {
  const { template } = synthesize({
    connector: {
      logDelivery: LOG_DELIVERY,
      schedule: ScheduleExpression.cron({
        hour: "2",
        weekDay: "SUN",
        timeZone: TimeZone.EUROPE_BERLIN,
      }),
    },
  });
  const request = requestOf(template);

  it("has one property, Request", () => {
    expect(
      Object.keys(propertiesOf(resourcesOf(template, "Custom::CcfCustomConnector")[0])).sort(),
    ).toEqual(["Request", "ServiceToken"]);
  });

  it("sends a CreateCustomConnector body the API's request schema accepts", () => {
    const { destination_id: destinationId, ...content } = request;

    expect(stringPattern("DestinationId").test(String(destinationId))).toBe(true);
    expect(
      schemaProblems({
        schema: componentSchema("CreateCustomConnectorRequestContent"),
        value: content,
        at: "$",
      }),
    ).toEqual([]);
  });

  it("sends the destination, the image by tag, the compute as numbers, and the configuration and secret", () => {
    expect(request).toMatchObject({
      destination_id: DESTINATION_ID,
      name: "github-awslabs",
      container_properties: {
        image_uri: `${TENANT_ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com/ccf-connectors/github:1.0.0`,
        command: ["ccf-connector", "run", "github_connector:GitHubConnectorImplementation"],
        resource_requirements: { cpu: 1, memory: 2048 },
        timeout: 900,
      },
      connector_configuration: '{"owner":"awslabs"}',
      source_access: { secret_arn: SECRET_ARN },
    });
  });

  it("sends the schedule enabled, with its expression and time zone", () => {
    expect(request.schedule).toEqual({
      expression: "cron(* 2 ? * SUN *)",
      state: "ENABLED",
      timezone: "Europe/Berlin",
    });
  });

  it("registers only after the image in its stack is built", () => {
    const { template: built, registrationId } = synthesize({ builtImage: true });
    const imageBuildId = onlyResourceId(built, "Custom::CcfConnectorImage");

    expect(built.toJSON().Resources[registrationId].DependsOn).toEqual(
      expect.arrayContaining([imageBuildId]),
    );
  });

  it.each([
    ["a space", "github awslabs"],
    ["65 characters", "a".repeat(65)],
  ])("rejects a name with %s, naming name", (_, name) => {
    expect(() => synthesize({ connector: { name } })).toThrow(`name "${name}" must match`);
  });
});

describe("CustomConnector's compute", () => {
  it("fails synthesis for 0.25 vCPU, naming compute.cpu", () => {
    expect(() => synthesize({ connector: { compute: { ...COMPUTE, cpu: 0.25 } } })).toThrow(
      "compute.cpu 0.25 must be from 0.5 to 16 vCPU.",
    );
  });

  it.each([
    [{ ...COMPUTE, memoryMib: 512 }, "compute.memoryMib"],
    [{ ...COMPUTE, timeout: Duration.seconds(30) }, "compute.timeout"],
    [{ ...COMPUTE, cpu: 0.5, memoryMib: 8192 }, "Fargate doesn't run"],
    [{ ...COMPUTE, cpu: 0.75 }, "Fargate doesn't run"],
    [{ ...COMPUTE, cpu: 8, memoryMib: 18432 }, "Fargate doesn't run"],
  ])("names the problem with %p", (compute, problem) => {
    expect(computeProblem(compute)).toContain(problem);
  });

  it.each([
    [0.5, 1024],
    [1, 8192],
    [2, 16384],
    [16, 122880],
  ])("accepts %p vCPU with %p MiB", (cpu, memoryMib) => {
    expect(computeProblem({ ...COMPUTE, cpu, memoryMib })).toBeUndefined();
  });
});

describe("CustomConnector with logDelivery", () => {
  const { stack, template, registrationId } = synthesize({
    connector: { logDelivery: LOG_DELIVERY },
  });
  const [logGroup] = resourcesOf(template, "AWS::Logs::LogGroup").filter(
    (group) => propertiesOf(group).LogGroupName === "/ccf/connectors/github-awslabs",
  );
  const [role] = roleStartingWith(template, TenantRolePrefix.LOG_DELIVERY);
  const logGroupArn = `logs:${Aws.REGION}:${TENANT_ACCOUNT}:log-group:/ccf/connectors/github-awslabs`;

  it("creates /ccf/connectors/<name> with the given retention and removal policy", () => {
    expect(propertiesOf(logGroup).RetentionInDays).toBe(30);
    expect(logGroup.DeletionPolicy).toBe("Retain");
  });

  it("deletes the log group with the stack when its removal policy is DESTROY", () => {
    const { template: disposable } = synthesize({
      connector: {
        logDelivery: { retention: RetentionDays.ONE_WEEK, removalPolicy: RemovalPolicy.DESTROY },
      },
    });
    const [group] = resourcesOf(disposable, "AWS::Logs::LogGroup").filter(
      (candidate) => propertiesOf(candidate).LogGroupName === "/ccf/connectors/github-awslabs",
    );

    expect(propertiesOf(group).RetentionInDays).toBe(7);
    expect(group.DeletionPolicy).toBe("Delete");
  });

  it("trusts the log-delivery role to the same runtime role and external ID", () => {
    expect(propertiesOf(role).AssumeRolePolicyDocument).toEqual(trustPolicy(registrationId));
  });

  it("lets the log-delivery role write only to that log group", () => {
    expect(propertiesOf(role).Policies).toEqual([
      {
        PolicyName: "CcfConnectorAccess",
        PolicyDocument: {
          Version: "2012-10-17",
          Statement: [
            {
              Sid: "WriteLogDestination",
              Effect: "Allow",
              Action: ["logs:CreateLogStream", "logs:PutLogEvents"],
              Resource: stack.resolve(`arn:${Aws.PARTITION}:${logGroupArn}:*`),
            },
          ],
        },
      },
    ]);
  });

  it("sends the log group and a role the API's LogDeliveryRoleArn pattern accepts", () => {
    const destination = requestOf(template).log_destination;

    expect(destination).toEqual({
      type: "CLOUDWATCH_LOGS",
      arn: `arn:aws:logs:us-east-1:${TENANT_ACCOUNT}:log-group:/ccf/connectors/github-awslabs`,
      role_arn: `arn:aws:iam::${TENANT_ACCOUNT}:role/CcfLogDeliveryRole-github-awslabs`,
    });
    expect(
      stringPattern("LogDeliveryRoleArn").test(
        String(isJsonObject(destination) ? destination.role_arn : ""),
      ),
    ).toBe(true);
  });
});

describe("CustomConnector without logDelivery", () => {
  const { template } = synthesize();

  it("creates no log group, no log-delivery role, and sends no log_destination", () => {
    expect(
      resourcesOf(template, "AWS::Logs::LogGroup").filter((group) =>
        String(propertiesOf(group).LogGroupName).startsWith("/ccf/connectors/"),
      ),
    ).toEqual([]);
    expect(roleStartingWith(template, TenantRolePrefix.LOG_DELIVERY)).toEqual([]);
    expect(requestOf(template).log_destination).toBeUndefined();
  });
});

describe("CustomConnector's tenant role names", () => {
  const longName = "a".repeat(30) + "-" + "b".repeat(33);

  it("shorten a 64-character connector name to 64 characters ending in 8 hex", () => {
    const { template } = synthesize({ connector: { name: longName, logDelivery: LOG_DELIVERY } });

    for (const prefix of [TenantRolePrefix.SOURCE_ACCESS, TenantRolePrefix.LOG_DELIVERY]) {
      const [role] = roleStartingWith(template, prefix);
      const roleName = String(propertiesOf(role).RoleName);

      expect(roleName).toHaveLength(64);
      expect(roleName).toMatch(/-[0-9a-f]{8}$/);
    }
  });
});

describe("CustomConnector's stack", () => {
  it("has no CloudWatch alarm, because a connector stack is client side", () => {
    synthesize().template.resourceCountIs("AWS::CloudWatch::Alarm", 0);
  });
});

describe("CustomConnector's provider", () => {
  it("is one per stack, however many custom connectors the stack has", () => {
    const stack = tenantStack("Connectors");
    const props = requiredProps(stack);
    const image = importedImage(stack);
    for (const name of ["github-awslabs", "github-aws-samples"])
      new CustomConnector(stack, name, { ...props, image, name });

    Template.fromStack(stack).resourcePropertiesCountIs(
      "AWS::Lambda::Function",
      { Handler: "register.on_event" },
      1,
    );
  });

  it("may call only the custom connector routes, with no list route", () => {
    const { stack, template } = synthesize();
    const route = (path: string): string =>
      JSON.stringify(
        partitionArn(stack, `execute-api:us-east-1:${SERVICE_ACCOUNT}:abcdef1234/*/${path}`),
      );
    const resources = resourcesOf(template, "AWS::IAM::Policy").flatMap((policy) =>
      statementsOf(propertiesOf(policy).PolicyDocument)
        .filter((statement) => statement.Action === "execute-api:Invoke")
        .flatMap((statement) =>
          Array.isArray(statement.Resource)
            ? statement.Resource.map((arn: unknown) => JSON.stringify(arn))
            : [],
        ),
    );

    expect([...new Set(resources)].sort()).toEqual(
      [
        "POST/api/v1/destinations/*/custom-connectors",
        "GET/api/v1/destinations/*/custom-connectors/*",
        "PUT/api/v1/destinations/*/custom-connectors/*",
        "DELETE/api/v1/destinations/*/custom-connectors/*",
      ]
        .map(route)
        .sort(),
    );
  });
});

describe("CustomConnector's permissions boundary", () => {
  const boundaryActions = (template: Template): unknown =>
    statementsOf(
      propertiesOf(resourcesOf(template, "AWS::IAM::ManagedPolicy")[0]).PolicyDocument,
    )[0].Action;

  it("bounds both roles to s3, secretsmanager, kms, and logs by default", () => {
    const { template } = synthesize({ connector: { logDelivery: LOG_DELIVERY } });
    const boundaryId = onlyResourceId(template, "AWS::IAM::ManagedPolicy");
    const boundary = propertiesOf(resourcesOf(template, "AWS::IAM::ManagedPolicy")[0]);

    expect(boundary.ManagedPolicyName).toBe("CcfConnectorBoundary-github-awslabs");
    expect(boundary.Path).toBe("/ccf/");
    expect(boundaryActions(template)).toEqual(["kms:*", "logs:*", "s3:*", "secretsmanager:*"]);
    for (const prefix of [TenantRolePrefix.SOURCE_ACCESS, TenantRolePrefix.LOG_DELIVERY]) {
      const [role] = roleStartingWith(template, prefix);
      expect(propertiesOf(role).PermissionsBoundary).toEqual({ Ref: boundaryId });
    }
  });

  it("adds each service in sourceAccessServices once", () => {
    const { template } = synthesize({ connector: { sourceAccessServices: ["dynamodb", "s3"] } });
    expect(boundaryActions(template)).toEqual([
      "dynamodb:*",
      "kms:*",
      "logs:*",
      "s3:*",
      "secretsmanager:*",
    ]);
  });

  it.each(["iam", "sts", "organizations"])(
    "rejects %s in sourceAccessServices, so a connector can't escalate its privileges",
    (service) => {
      expect(() => synthesize({ connector: { sourceAccessServices: [service] } })).toThrow(
        `sourceAccessServices[0] "${service}" is not allowed`,
      );
    },
  );

  it.each(["DynamoDB", "s3:*", "", "dynamo db", "-s3"])(
    "rejects %p, which isn't a lowercase service prefix",
    (service) => {
      expect(() => synthesize({ connector: { sourceAccessServices: [service] } })).toThrow(
        "must be a lowercase IAM service prefix",
      );
    },
  );
});
