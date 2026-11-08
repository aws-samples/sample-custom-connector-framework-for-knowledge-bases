import { Duration, RemovalPolicy, Stack } from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { RetentionDays } from "aws-cdk-lib/aws-logs";
import { Destination, DestinationProps } from "../lib/constructs/destination";
import { DestinationRole } from "../lib/constructs/destination-role";
import {
  BUCKET_NAME,
  DESTINATION_ID,
  JsonObject,
  SERVICE_ACCOUNT,
  literalFramework,
  partitionArn,
  propertiesOf,
  resourcesOf,
  statementsOf,
  tenantStack,
} from "./construct-fixtures";

type SynthesizeRequest = Partial<Omit<DestinationProps, "framework" | "role" | "name">>;

interface Synthesized {
  readonly stack: Stack;
  readonly template: Template;
  readonly construct: Destination;
  readonly destination: JsonObject;
}

/** The logical ID of the destination role; the providers have roles too. */
const destinationRoleId = (template: Template): string =>
  Object.keys(
    template.findResources("AWS::IAM::Role", {
      Properties: { RoleName: "CcfDestinationRole-GitHub" },
    }),
  )[0];

const synthesize = (request: SynthesizeRequest = {}): Synthesized => {
  const stack = tenantStack();
  const framework = literalFramework();
  const role = new DestinationRole(stack, "Role", {
    framework,
    destinationName: "GitHub",
    removalPolicy: RemovalPolicy.RETAIN,
  });
  const construct = new Destination(stack, "Destination", {
    framework,
    role,
    name: "GitHub",
    type: "BEDROCK",
    accessControlEnabled: false,
    logRetention: RetentionDays.ONE_MONTH,
    removalPolicy: RemovalPolicy.RETAIN,
    totalTimeout: Duration.hours(1),
    ...request,
  });
  const template = Template.fromStack(stack);
  const [destination] = resourcesOf(template, "Custom::CcfDestination");
  return { stack, template, construct, destination };
};

describe("Destination", () => {
  const { stack, template, destination } = synthesize();

  it("sends the name, type, the role's ARN, and access control", () => {
    expect(propertiesOf(destination)).toMatchObject({
      Name: "GitHub",
      Type: "BEDROCK",
      AccessControlEnabled: false,
      RoleArn: { "Fn::GetAtt": [destinationRoleId(template), "Arn"] },
      MetadataFields: [],
    });
    expect(propertiesOf(synthesize({ accessControlEnabled: true }).destination)).toMatchObject({
      AccessControlEnabled: true,
    });
  });

  it("sends each metadata field in CloudFormation's PascalCase", () => {
    const owner = { name: "owner", type: "STRING", searchability: "ENABLED" };

    expect(propertiesOf(synthesize({ metadataFields: [owner] }).destination)).toMatchObject({
      MetadataFields: [{ Name: "owner", Type: "STRING", Searchability: "ENABLED" }],
    });
  });

  const field = (name: string, type = "STRING") => ({ name, type, searchability: "DISABLED" });
  it.each<[string, SynthesizeRequest, RegExp]>([
    ["fields on an S3 destination", { type: "S3", metadataFields: [field("owner")] }, /BEDROCK/],
    ["a name sent twice", { metadataFields: [field("owner"), field("owner")] }, /each field once/],
    ["a reserved name", { metadataFields: [field("_source_uri")] }, /"_source_uri" must be/],
    ["an unknown type", { metadataFields: [field("stars", "NUMBER")] }, /STRING, STRING_LIST/],
    [
      "31 fields",
      { metadataFields: Array.from({ length: 31 }, (_, index) => field(`field${index}`)) },
      /at most 30/,
    ],
  ])("fails synthesis on %s", (_case, request, message) => {
    expect(() => synthesize(request)).toThrow(message);
  });

  it("depends on its role and both boundaries, so it is deleted before them", () => {
    const boundaryIds = Object.keys(template.findResources("AWS::IAM::ManagedPolicy"));

    expect(boundaryIds).toHaveLength(2);
    expect(destination.DependsOn).toEqual(
      expect.arrayContaining([destinationRoleId(template), ...boundaryIds]),
    );
  });

  it("takes its removal policy from the props", () => {
    expect([destination.DeletionPolicy, destination.UpdateReplacePolicy]).toEqual([
      "Retain",
      "Retain",
    ]);
    expect(synthesize({ removalPolicy: RemovalPolicy.DESTROY }).destination.DeletionPolicy).toBe(
      "Delete",
    );
  });

  it("lets its provider call only the destination routes, with no list route", () => {
    const route = (path: string): string =>
      JSON.stringify(
        partitionArn(stack, `execute-api:us-east-1:${SERVICE_ACCOUNT}:abcdef1234/*/${path}`),
      );
    const resources = resourcesOf(template, "AWS::IAM::Policy").flatMap((policy) =>
      statementsOf(propertiesOf(policy).PolicyDocument)
        .filter((statement) => statement.Action === "execute-api:Invoke")
        .flatMap((statement) =>
          (statement.Resource as unknown[]).map((arn) => JSON.stringify(arn)),
        ),
    );

    expect([...new Set(resources)].sort()).toEqual(
      [
        "POST/api/v1/destinations",
        "GET/api/v1/destinations/*",
        "PUT/api/v1/destinations/*",
        "DELETE/api/v1/destinations/*",
      ]
        .map(route)
        .sort(),
    );
  });

  it("gives the provider's waiter the configured timeout, polling every 30 seconds", () => {
    const maxAttempts = (timeout: Duration): string => {
      const [waiter] = resourcesOf(
        synthesize({ totalTimeout: timeout }).template,
        "AWS::StepFunctions::StateMachine",
      );
      const definition = JSON.stringify(propertiesOf(waiter).DefinitionString);
      return /\\"MaxAttempts\\":(\d+)/.exec(definition)?.[1] ?? "";
    };

    expect(maxAttempts(Duration.hours(1))).toBe("120");
    expect(maxAttempts(Duration.hours(2))).toBe("240");
  });

  it("keeps its provider's log records for the configured retention", () => {
    const retentions = new Set(
      resourcesOf(
        synthesize({ logRetention: RetentionDays.ONE_WEEK }).template,
        "AWS::Logs::LogGroup",
      ).map((group) => propertiesOf(group).RetentionInDays),
    );

    expect(retentions).toEqual(new Set([7]));
  });

  it("has knowledge base IDs for BEDROCK and none for S3", () => {
    expect(synthesize().construct.knowledgeBaseId).toBeDefined();
    const s3 = synthesize({ type: "S3" }).construct;

    expect([s3.knowledgeBaseId, s3.dataSourceId]).toEqual([undefined, undefined]);
  });

  it("fails synthesis on a type the API doesn't have, naming type", () => {
    expect(() => synthesize({ type: "KENDRA" })).toThrow(/type "KENDRA" must be one of/);
  });
});

describe("Destination imports", () => {
  it("fromDestinationAttributes returns the attributes, no knowledge base IDs when unset, and creates nothing", () => {
    const stack = tenantStack();
    const imported = Destination.fromDestinationAttributes(stack, "Imported", {
      destinationId: DESTINATION_ID,
      bucketName: BUCKET_NAME,
    });

    expect(imported).toEqual({
      destinationId: DESTINATION_ID,
      bucketName: BUCKET_NAME,
      knowledgeBaseId: undefined,
      dataSourceId: undefined,
    });
    expect(Template.fromStack(stack).toJSON().Resources ?? {}).toEqual({});
  });

  it("fromDestinationId builds the bucket name from the ID, the stack's account, and the service Region", () => {
    const imported = Destination.fromDestinationId(tenantStack(), "Imported", {
      destinationId: DESTINATION_ID,
      framework: literalFramework(),
    });

    expect(imported.bucketName).toBe(BUCKET_NAME);
  });

  it.each(["dst-123", "cc-7c41e9a2b5d0", "dst-7C41E9A2B5D0"])(
    "rejects the destination ID %s, naming destinationId",
    (destinationId) => {
      expect(() =>
        Destination.fromDestinationAttributes(tenantStack(), "Imported", {
          destinationId,
          bucketName: BUCKET_NAME,
        }),
      ).toThrow(/destinationId/);
    },
  );
});
