import { Aws } from "aws-cdk-lib";
import { destinationBucketPolicy } from "custom-connector-framework-cdk";
import {
  ACCOUNT,
  JsonObject,
  SERVICE_ACCOUNT,
  SynthesizedApp,
  partitionArn,
  propertiesOf,
  resourcesOf,
  rolesStartingWith,
  synthesize,
} from "./github-app-fixtures";

/** The custom resource's attribute as the connector stack references it. */
const attribute = (registrationId: string, name: string): unknown => ({
  "Fn::GetAtt": [registrationId, name],
});

/** The registration whose `Request` names the custom connector. */
const registrationIdOf = (app: SynthesizedApp, connectorName: string): string => {
  const [id] = Object.entries(app.connector.findResources("Custom::CcfCustomConnector"))
    .filter(([, registration]) =>
      JSON.stringify(propertiesOf(registration).Request).includes(
        `\\"name\\":\\"${connectorName}\\"`,
      ),
    )
    .map(([logicalId]) => logicalId);
  return id;
};

/** Trust only the custom connector's runtime role, pinned by ARN, with its external ID. */
const runtimeRoleTrust = (registrationId: string, accountPrincipal: unknown): unknown => ({
  Version: "2012-10-17",
  Statement: [
    {
      Effect: "Allow",
      Principal: { AWS: accountPrincipal },
      Action: "sts:AssumeRole",
      Condition: {
        StringEquals: { "sts:ExternalId": attribute(registrationId, "ExternalId") },
        ArnEquals: { "aws:PrincipalArn": attribute(registrationId, "RuntimeRoleArn") },
      },
    },
  ],
});

const onlyRole = (roles: JsonObject[]): JsonObject => {
  expect(roles).toHaveLength(1);
  return roles[0];
};

/** The service account's root principal: imported with default.json, literal with example-small.json. */
const serviceRoot = (file: string): unknown =>
  file === "default.json"
    ? {
        "Fn::Join": [
          "",
          [
            "arn:aws:iam::",
            { "Fn::ImportValue": "CustomConnectorFramework-ServiceAccountId" },
            ":root",
          ],
        ],
      }
    : `arn:aws:iam::${SERVICE_ACCOUNT}:root`;

const inlineStatements = (role: JsonObject): JsonObject[] => {
  const policies = propertiesOf(role).Policies as JsonObject[];
  expect(policies).toHaveLength(1);
  return (policies[0].PolicyDocument as JsonObject).Statement as JsonObject[];
};

describe.each([
  ["default.json", "dst-0123456789ab"],
  ["example-small.json", "dst-7c41e9a2b5d0"],
])("policies with %s", (file, destinationId) => {
  let app: SynthesizedApp;
  let registrationId: string;
  let sourceRole: JsonObject;
  let sourceRoleId: string;

  beforeAll(() => {
    app = synthesize(file);
    registrationId = registrationIdOf(app, "github-aws-samples");
    sourceRole = onlyRole(rolesStartingWith(app.connector, "CcfSourceRole-github-aws-samples"));
    sourceRoleId = Object.keys(
      app.connector.findResources("AWS::IAM::Role", {
        Properties: { RoleName: "CcfSourceRole-github-aws-samples" },
      }),
    )[0];
  });

  it("trusts the source-access role only to the runtime role with the external ID", () => {
    expect(propertiesOf(sourceRole).AssumeRolePolicyDocument).toEqual(
      runtimeRoleTrust(registrationId, serviceRoot(file)),
    );
  });

  it("gives the source-access role its own connector_id= prefix of the destination bucket inline", () => {
    const connectorId = attribute(registrationId, "ConnectorId");
    const statements = inlineStatements(sourceRole);
    const bucketJson = JSON.stringify(statements);

    expect(statements.map((statement) => [statement.Sid, statement.Action])).toEqual([
      ["ConnectorPrefixObjects", ["s3:DeleteObject", "s3:GetObject", "s3:PutObject"]],
      ["ConnectorPrefixList", "s3:ListBucket"],
    ]);
    expect(bucketJson).toContain(`ccf-${destinationId}-${ACCOUNT}-`);
    expect(statements[1].Condition).toEqual({
      StringLike: { "s3:prefix": { "Fn::Join": ["", ["connector_id=", connectorId, "/*"]] } },
    });
  });

  it("grants the source-access role read access to the token secret, and only it", () => {
    const [secretId] = Object.keys(app.connector.findResources("AWS::SecretsManager::Secret"));
    const attached = resourcesOf(app.connector, "AWS::IAM::Policy").filter((policy) =>
      JSON.stringify(propertiesOf(policy).Roles).includes(sourceRoleId),
    );

    expect(attached).toHaveLength(1);
    expect((propertiesOf(attached[0]).PolicyDocument as JsonObject).Statement).toEqual([
      {
        Effect: "Allow",
        Action: ["secretsmanager:DescribeSecret", "secretsmanager:GetSecretValue"],
        Resource: { Ref: secretId },
      },
    ]);
  });

  it("lets only the framework's Batch execution role pull from the image repository", () => {
    const [repository] = resourcesOf(app.connector, "AWS::ECR::Repository");
    const batchExecutionRole =
      file === "default.json"
        ? { "Fn::ImportValue": "CustomConnectorFramework-BatchExecutionRoleArn" }
        : `arn:aws:iam::${SERVICE_ACCOUNT}:role/ccf/CcfBatchExecutionRole`;

    expect(propertiesOf(repository).RepositoryPolicyText).toEqual({
      Version: "2012-10-17",
      Statement: [
        {
          Effect: "Allow",
          Principal: { AWS: batchExecutionRole },
          Action: [
            "ecr:BatchCheckLayerAvailability",
            "ecr:BatchGetImage",
            "ecr:GetDownloadUrlForLayer",
          ],
        },
      ],
    });
  });
});

describe("policies with default.json only", () => {
  it("trusts the log-delivery role to the runtime role and lets it write only its log group", () => {
    const app = synthesize("default.json");
    const registrationId = registrationIdOf(app, "github-awslabs");
    const role = onlyRole(rolesStartingWith(app.connector, "CcfLogDeliveryRole-github-awslabs"));

    expect(propertiesOf(role).AssumeRolePolicyDocument).toEqual(
      runtimeRoleTrust(registrationId, serviceRoot("default.json")),
    );
    expect(inlineStatements(role)).toEqual([
      {
        Sid: "WriteLogDestination",
        Effect: "Allow",
        Action: ["logs:CreateLogStream", "logs:PutLogEvents"],
        Resource: partitionArn(
          app.stack,
          `logs:us-east-1:${ACCOUNT}:log-group:/ccf/connectors/github-awslabs:*`,
        ),
      },
    ]);
  });
});

describe("the destination bucket policy the framework writes", () => {
  it("grants nothing to the service account or any other principal", () => {
    const bucketArn = `arn:${Aws.PARTITION}:s3:::ccf-dst-7c41e9a2b5d0-111122223333-us-east-1`;
    const policy = destinationBucketPolicy({ bucketArn, objectsArn: `${bucketArn}/*` });

    expect(policy).toEqual({
      Version: "2012-10-17",
      Statement: [
        {
          Sid: "DenyInsecureTransport",
          Effect: "Deny",
          Principal: "*",
          Action: "s3:*",
          Resource: [
            `arn:${Aws.PARTITION}:s3:::ccf-dst-7c41e9a2b5d0-111122223333-us-east-1`,
            `arn:${Aws.PARTITION}:s3:::ccf-dst-7c41e9a2b5d0-111122223333-us-east-1/*`,
          ],
          Condition: { Bool: { "aws:SecureTransport": "false" } },
        },
      ],
    });
  });
});
