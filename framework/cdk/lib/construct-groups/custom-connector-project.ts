import { Duration, RemovalPolicy } from "aws-cdk-lib";
import { RetentionDays } from "aws-cdk-lib/aws-logs";
import { Construct } from "constructs";
import { ConnectorImage } from "../constructs/connector-image";
import { CustomConnector, CustomConnectorProps } from "../constructs/custom-connector";
import { IDestination } from "../constructs/destination";
import { FrameworkValues } from "../constructs/framework-values";

/** Where the project's connector image builds from, the repository it goes to, and how long it lives. */
export interface CustomConnectorProjectImageProps {
  /** Must start with `ccf-connectors/`. */
  readonly repositoryName: string;
  /** The Docker build context directory. */
  readonly directory: string;
  /** The Dockerfile path, relative to `directory`. */
  readonly file: string;
  /** How long after its push every image expires; see `ConnectorImageProps.maxImageAge`. */
  readonly maxImageAge: Duration;
  /** How long one image build may run. */
  readonly buildTimeout: Duration;
}

export interface CustomConnectorProjectProps {
  readonly framework: FrameworkValues;
  /** An existing destination, from `Destination.fromDestinationId` or `fromDestinationAttributes`. */
  readonly destination: IDestination;
  readonly image: CustomConnectorProjectImageProps;
  /** For the image repository. */
  readonly removalPolicy: RemovalPolicy;
  /** For the build's and the providers' log groups. */
  readonly logRetention: RetentionDays;
}

/** A custom connector in the project: its construct ID and everything but the parts the project holds. */
export interface AddCustomConnectorRequest extends Omit<
  CustomConnectorProps,
  "framework" | "destination" | "image" | "logRetention"
> {
  readonly id: string;
}

/**
 * A connector project's shared parts: one `ConnectorImage` that every custom connector in it runs,
 * an existing destination they all write to, and the framework values. `addCustomConnector` adds
 * one `CustomConnector` per source. The project never creates a destination; a
 * `CustomConnectorDestinationFor<Name>` stack does.
 */
export class CustomConnectorProject extends Construct {
  public readonly framework: FrameworkValues;
  public readonly destination: IDestination;
  public readonly image: ConnectorImage;
  private readonly logRetention: RetentionDays;

  constructor(scope: Construct, id: string, props: CustomConnectorProjectProps) {
    const { framework, destination, image, removalPolicy, logRetention } = props;
    super(scope, id);
    this.framework = framework;
    this.destination = destination;
    this.logRetention = logRetention;
    this.image = new ConnectorImage(this, "Image", {
      ...image,
      framework,
      removalPolicy,
      logRetention,
    });
  }

  /** One custom connector that runs the project's image and writes to its destination. */
  public addCustomConnector(request: AddCustomConnectorRequest): CustomConnector {
    const { id, ...props } = request;
    return new CustomConnector(this, id, {
      ...props,
      framework: this.framework,
      destination: this.destination,
      image: this.image,
      logRetention: this.logRetention,
    });
  }
}
