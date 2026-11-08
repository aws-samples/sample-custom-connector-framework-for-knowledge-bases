import { CfnOutput, Duration, RemovalPolicy, Stack, StackProps } from "aws-cdk-lib";
import { RetentionDays } from "aws-cdk-lib/aws-logs";
import { Construct } from "constructs";
import { CustomConnectorDestination } from "../construct-groups/custom-connector-destination";
import { MetadataFieldProps } from "../constructs/destination";
import { FrameworkValues } from "../constructs/framework-values";

const STACK_NAME_PREFIX = "CustomConnectorDestinationFor";

/** The stack's outputs; the first three are also exported as `<stack name>-<output>`. */
export enum DestinationOutput {
  DESTINATION_ID = "DestinationId",
  BUCKET_NAME = "BucketName",
  KNOWLEDGE_BASE_ID = "KnowledgeBaseId",
  DATA_SOURCE_ID = "DataSourceId",
  ROLE_ARN = "DestinationRoleArn",
}

/** The outputs another stack imports, such as the MCP stack's knowledge base ID. */
const EXPORTED_OUTPUTS: ReadonlySet<DestinationOutput> = new Set([
  DestinationOutput.DESTINATION_ID,
  DestinationOutput.BUCKET_NAME,
  DestinationOutput.KNOWLEDGE_BASE_ID,
]);

/**
 * `CustomConnectorDestinationFor<Name>`: the destination name in PascalCase, each part between
 * `.`, `_`, or `-` starting with a capital and keeping its other letters, so `GitHub` stays
 * `GitHub` and `github-open-source` becomes `GithubOpenSource`.
 */
export const destinationStackName = (destinationName: string): string =>
  STACK_NAME_PREFIX +
  destinationName
    .split(/[._-]+/)
    .filter((part) => part.length > 0)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join("");

export interface CustomConnectorDestinationStackProps extends StackProps {
  readonly framework: FrameworkValues;
  readonly destinationName: string;
  readonly destinationType: string;
  readonly accessControlEnabled: boolean;
  /** Unset is none. */
  readonly metadataFields?: readonly MetadataFieldProps[];
  readonly removalPolicy: RemovalPolicy;
  readonly logRetention: RetentionDays;
  /** How long the destination provider waits for the destination to become `ACTIVE`, or to be gone. */
  readonly providerTimeout: Duration;
}

/** One destination and the role and boundaries it owns, in its own stack, with its IDs as outputs. */
export class CustomConnectorDestinationStack extends Stack {
  public readonly destination: CustomConnectorDestination;

  constructor(scope: Construct, id: string, props: CustomConnectorDestinationStackProps) {
    super(scope, id, props);
    const {
      framework,
      destinationName,
      destinationType,
      accessControlEnabled,
      metadataFields,
      removalPolicy,
      logRetention,
      providerTimeout,
    } = props;
    this.destination = new CustomConnectorDestination(this, "Destination", {
      framework,
      name: destinationName,
      type: destinationType,
      accessControlEnabled,
      metadataFields,
      removalPolicy,
      logRetention,
      providerTimeout,
    });
    const { destinationId, bucketName, knowledgeBaseId, dataSourceId } =
      this.destination.destination;
    const outputs = new Map<DestinationOutput, string>([
      [DestinationOutput.DESTINATION_ID, destinationId],
      [DestinationOutput.BUCKET_NAME, bucketName],
      [DestinationOutput.ROLE_ARN, this.destination.role.roleArn],
    ]);
    if (knowledgeBaseId !== undefined)
      outputs.set(DestinationOutput.KNOWLEDGE_BASE_ID, knowledgeBaseId);
    if (dataSourceId !== undefined) outputs.set(DestinationOutput.DATA_SOURCE_ID, dataSourceId);
    for (const [output, value] of outputs)
      new CfnOutput(this, output, {
        value,
        exportName: EXPORTED_OUTPUTS.has(output) ? `${this.stackName}-${output}` : undefined,
      });
  }
}
