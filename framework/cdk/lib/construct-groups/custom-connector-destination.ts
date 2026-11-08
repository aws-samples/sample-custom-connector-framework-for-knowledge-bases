import { Duration, RemovalPolicy } from "aws-cdk-lib";
import { RetentionDays } from "aws-cdk-lib/aws-logs";
import { Construct } from "constructs";
import { Destination, MetadataFieldProps } from "../constructs/destination";
import { DestinationRole } from "../constructs/destination-role";
import { FrameworkValues } from "../constructs/framework-values";

export interface CustomConnectorDestinationProps {
  readonly framework: FrameworkValues;
  readonly name: string;
  /** One of the API's `DestinationType` values. */
  readonly type: string;
  /** Whether the `BEDROCK` data source enforces access control lists. Fixed at creation. */
  readonly accessControlEnabled: boolean;
  /** The `BEDROCK` knowledge base's custom metadata fields; unset is none. A change updates the destination in place. */
  readonly metadataFields?: readonly MetadataFieldProps[];
  /** For the destination, its role, and both boundaries. */
  readonly removalPolicy: RemovalPolicy;
  /** For the destination provider's log groups. */
  readonly logRetention: RetentionDays;
  /** How long the provider waits for the destination to become `ACTIVE`, or to be gone. */
  readonly providerTimeout: Duration;
}

/**
 * One destination with everything it owns: its own `DestinationRole` and boundaries, and the
 * destination the service builds through that role (bucket, and for `BEDROCK` the knowledge base,
 * data source, and knowledge base role). Connector stacks reference the destination by ID and
 * never create one.
 */
export class CustomConnectorDestination extends Construct {
  public readonly role: DestinationRole;
  public readonly destination: Destination;

  constructor(scope: Construct, id: string, props: CustomConnectorDestinationProps) {
    const {
      framework,
      name,
      type,
      accessControlEnabled,
      metadataFields,
      removalPolicy,
      logRetention,
      providerTimeout,
    } = props;
    super(scope, id);
    this.role = new DestinationRole(this, "Role", {
      framework,
      destinationName: name,
      removalPolicy,
    });
    this.destination = new Destination(this, "Destination", {
      framework,
      role: this.role,
      name,
      type,
      accessControlEnabled,
      metadataFields,
      removalPolicy,
      logRetention,
      totalTimeout: providerTimeout,
    });
  }
}
