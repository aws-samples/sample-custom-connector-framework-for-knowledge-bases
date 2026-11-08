import { Duration, RemovalPolicy } from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { RetentionDays } from "aws-cdk-lib/aws-logs";
import { CustomConnectorProject } from "../lib/construct-groups/custom-connector-project";
import { Destination } from "../lib/constructs/destination";
import {
  BUCKET_NAME,
  DESTINATION_ID,
  IMAGE_DIRECTORY,
  literalFramework,
  nagViolations,
  onlyResourceId,
  propertiesOf,
  resourcesOf,
  tenantStack,
} from "./construct-fixtures";

/** The custom connector props a project leaves to each `addCustomConnector` call, but its name. */
const CONNECTOR = {
  command: ["ccf-connector"],
  compute: { cpu: 1, memoryMib: 2048, timeout: Duration.minutes(15) },
  logDelivery: { retention: RetentionDays.ONE_MONTH, removalPolicy: RemovalPolicy.RETAIN },
  deletionTimeout: Duration.hours(1),
  sourceAccessServices: [],
};

/** The image settings every project in these tests uses. */
const IMAGE = {
  repositoryName: "ccf-connectors/github",
  directory: IMAGE_DIRECTORY,
  file: "Dockerfile",
  maxImageAge: Duration.days(90),
  buildTimeout: Duration.hours(1),
};

const synthesize = (): Template => {
  const stack = tenantStack("Project");
  const framework = literalFramework();
  const project = new CustomConnectorProject(stack, "Project", {
    framework,
    destination: Destination.fromDestinationId(stack, "Destination", {
      destinationId: DESTINATION_ID,
      framework,
    }),
    image: IMAGE,
    removalPolicy: RemovalPolicy.RETAIN,
    logRetention: RetentionDays.ONE_MONTH,
  });
  for (const owner of ["awslabs", "aws-samples"])
    project.addCustomConnector({ ...CONNECTOR, id: owner, name: `github-${owner}` });
  return Template.fromStack(stack);
};

describe("CustomConnectorProject", () => {
  const template = synthesize();

  it("builds one image and registers every custom connector with it", () => {
    const imageBuildId = onlyResourceId(template, "Custom::CcfConnectorImage");
    const registrations = resourcesOf(template, "Custom::CcfCustomConnector");

    expect(resourcesOf(template, "AWS::ECR::Repository")).toHaveLength(1);
    expect(registrations).toHaveLength(2);
    for (const registration of registrations)
      expect(registration.DependsOn).toEqual(expect.arrayContaining([imageBuildId]));
  });

  it("registers every custom connector in the referenced destination and creates no destination", () => {
    expect(resourcesOf(template, "Custom::CcfDestination")).toEqual([]);
    expect(
      resourcesOf(template, "Custom::CcfCustomConnector").map((registration) =>
        JSON.stringify(propertiesOf(registration).Request).includes(
          `destination_id\\":\\"${DESTINATION_ID}`,
        ),
      ),
    ).toEqual([true, true]);
    expect(JSON.stringify(template.toJSON())).toContain(BUCKET_NAME);
  });

  it("gives each custom connector its own source-access and log-delivery roles", () => {
    const roleNames = resourcesOf(template, "AWS::IAM::Role")
      .map((role) => String(propertiesOf(role).RoleName))
      .filter((name) => name.startsWith("Ccf"))
      .sort();

    expect(roleNames).toEqual([
      "CcfLogDeliveryRole-github-aws-samples",
      "CcfLogDeliveryRole-github-awslabs",
      "CcfSourceRole-github-aws-samples",
      "CcfSourceRole-github-awslabs",
    ]);
  });

  it("is clean under cdk-nag", () => {
    const stack = tenantStack("Nagged");
    const framework = literalFramework();
    new CustomConnectorProject(stack, "Project", {
      framework,
      destination: Destination.fromDestinationId(stack, "Destination", {
        destinationId: DESTINATION_ID,
        framework,
      }),
      image: IMAGE,
      removalPolicy: RemovalPolicy.RETAIN,
      logRetention: RetentionDays.ONE_MONTH,
    }).addCustomConnector({ ...CONNECTOR, id: "awslabs", name: "github-awslabs" });

    expect(nagViolations(stack)).toEqual({});
  });
});
