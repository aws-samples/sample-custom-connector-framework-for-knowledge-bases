import { Duration, RemovalPolicy } from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import { RetentionDays } from "aws-cdk-lib/aws-logs";
import { FrameworkExport } from "../lib/common/framework-exports";
import { FrameworkValues } from "../lib/constructs/framework-values";
import {
  CustomConnectorDestinationStack,
  CustomConnectorDestinationStackProps,
  destinationStackName,
} from "../lib/stacks/custom-connector-destination-stack";
import {
  TENANT_ENV,
  createApp,
  importedNames,
  literalFramework,
  nagViolations,
  onlyResourceId,
  propertiesOf,
  resourcesOf,
} from "./construct-fixtures";

const synthesize = (
  overrides: Partial<CustomConnectorDestinationStackProps> = {},
): CustomConnectorDestinationStack =>
  new CustomConnectorDestinationStack(createApp(), "CustomConnectorDestinationForGitHub", {
    env: TENANT_ENV,
    framework: literalFramework(),
    destinationName: "GitHub",
    destinationType: "BEDROCK",
    accessControlEnabled: false,
    removalPolicy: RemovalPolicy.RETAIN,
    logRetention: RetentionDays.ONE_MONTH,
    providerTimeout: Duration.hours(1),
    ...overrides,
  });

const outputNames = (template: Template): string[] =>
  Object.keys(template.toJSON().Outputs as Record<string, unknown>).sort();

/** Each output's export name, by output; an output without one is left out. */
const exportNames = (template: Template): Record<string, unknown> =>
  Object.fromEntries(
    Object.entries(template.findOutputs("*"))
      .filter(([, output]) => output.Export !== undefined)
      .map(([name, output]) => [name, output.Export.Name]),
  );

describe("destinationStackName", () => {
  it.each([
    ["GitHub", "CustomConnectorDestinationForGitHub"],
    ["engineering", "CustomConnectorDestinationForEngineering"],
    ["github-open-source", "CustomConnectorDestinationForGithubOpenSource"],
    ["team_docs.v2", "CustomConnectorDestinationForTeamDocsV2"],
  ])("names the stack for %s %s", (name, stackName) => {
    expect(destinationStackName(name)).toBe(stackName);
  });
});

describe("CustomConnectorDestinationStack", () => {
  const template = Template.fromStack(synthesize());

  it("holds the destination role, its two boundaries, and the destination together", () => {
    expect(resourcesOf(template, "AWS::IAM::ManagedPolicy")).toHaveLength(2);
    expect(
      resourcesOf(template, "AWS::IAM::Role").filter((role) =>
        String(propertiesOf(role).RoleName).startsWith("CcfDestinationRole-"),
      ),
    ).toHaveLength(1);
    expect(resourcesOf(template, "Custom::CcfDestination")).toHaveLength(1);
  });

  it("registers the destination with the stack's own role", () => {
    const [roleId] = Object.keys(
      template.findResources("AWS::IAM::Role", {
        Properties: { RoleName: Match.stringLikeRegexp("^CcfDestinationRole-") },
      }),
    );
    const [destination] = resourcesOf(template, "Custom::CcfDestination");

    expect(propertiesOf(destination).RoleArn).toEqual({ "Fn::GetAtt": [roleId, "Arn"] });
  });

  it("imports nothing from another destination stack or the service with literal framework values", () => {
    expect(importedNames(template)).toEqual([]);
  });

  it("reads only the four framework exports and the REST API ID in the service account", () => {
    const inServiceAccount = Template.fromStack(
      synthesize({ framework: FrameworkValues.fromConfig() }),
    );

    expect(importedNames(inServiceAccount)).toEqual(
      [
        FrameworkExport.API_ENDPOINT,
        FrameworkExport.SERVICE_ACCOUNT_ID,
        FrameworkExport.DESTINATION_WORKFLOW_ARN,
        FrameworkExport.REST_API_ID,
      ].sort(),
    );
  });

  it("outputs the destination ID, bucket, role, and the knowledge base IDs for BEDROCK", () => {
    expect(outputNames(template)).toEqual([
      "BucketName",
      "DataSourceId",
      "DestinationId",
      "DestinationRoleArn",
      "KnowledgeBaseId",
    ]);
  });

  it("outputs no knowledge base IDs for an S3 destination", () => {
    const s3 = Template.fromStack(synthesize({ destinationType: "S3" }));

    expect(outputNames(s3)).toEqual(["BucketName", "DestinationId", "DestinationRoleArn"]);
  });

  it("exports the destination ID, bucket, and knowledge base ID under the stack's name", () => {
    expect(exportNames(template)).toEqual({
      DestinationId: "CustomConnectorDestinationForGitHub-DestinationId",
      BucketName: "CustomConnectorDestinationForGitHub-BucketName",
      KnowledgeBaseId: "CustomConnectorDestinationForGitHub-KnowledgeBaseId",
    });
  });

  it("applies the removal policy to the destination, the role, and both boundaries", () => {
    const destroyed = Template.fromStack(synthesize({ removalPolicy: RemovalPolicy.DESTROY }));
    const resources = destroyed.toJSON().Resources as Record<string, { DeletionPolicy?: string }>;
    const policies = [
      onlyResourceId(destroyed, "Custom::CcfDestination"),
      ...Object.keys(
        destroyed.findResources("AWS::IAM::Role", {
          Properties: { RoleName: Match.stringLikeRegexp("^CcfDestinationRole-") },
        }),
      ),
      ...Object.keys(destroyed.findResources("AWS::IAM::ManagedPolicy")),
    ].map((id) => resources[id].DeletionPolicy);

    expect(policies).toEqual(["Delete", "Delete", "Delete", "Delete"]);
  });

  it("is clean under cdk-nag", () => {
    expect(nagViolations(synthesize())).toEqual({});
  });
});
