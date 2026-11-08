import { RemovalPolicy } from "aws-cdk-lib";
import { Key } from "aws-cdk-lib/aws-kms";
import { Construct } from "constructs";
import { ProjectName, ResourceName, createResourceName } from "../common/config";

export interface ServiceKeyProps {
  /** Project name used to derive the key alias. */
  readonly projectName: ProjectName;
  /** Removal policy applied to the key on stack deletion. */
  readonly removalPolicy: RemovalPolicy;
}

/**
 * The customer managed key that encrypts every stateful framework resource. One key means one
 * `DisableKey` call stops all access. Rotation is on.
 */
export class ServiceKey extends Construct {
  public readonly key: Key;

  constructor(scope: Construct, id: string, props: ServiceKeyProps) {
    super(scope, id);
    const { projectName, removalPolicy } = props;
    this.key = new Key(this, ResourceName.SERVICE_KEY, {
      alias: createResourceName({ projectName, resourceName: ResourceName.SERVICE_KEY }),
      description:
        "Custom Connector Framework service key: the six tables, every framework queue, and the envelope " +
        "encryption of connector configurations, job configurations, and checkpoints.",
      enableKeyRotation: true,
      removalPolicy,
    });
  }
}
