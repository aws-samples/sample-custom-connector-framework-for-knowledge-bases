import { RemovalPolicy } from "aws-cdk-lib";
import { Grant, IGrantable } from "aws-cdk-lib/aws-iam";
import { Key } from "aws-cdk-lib/aws-kms";
import { Construct } from "constructs";
import { ProjectName, ResourceName, createConstructId, createResourceName } from "../common/config";

/**
 * Configuration for the {@link Encryption} construct group.
 */
export interface EncryptionProps {
  /** Project name used to derive the key alias. */
  readonly projectName: ProjectName;
  /** Removal policy applied to the key on stack deletion. */
  readonly removalPolicy: RemovalPolicy;
}

/**
 * The single customer-managed key that encrypts every stateful resource in the
 * framework — the DynamoDB tables, the S3 ingestion bucket, and (when the
 * Bedrock provider is selected) the S3 Vectors store. Routing all encryption
 * through one CMK means a single `DisableKey` call is the service's andon cord
 * (secure-infrastructure rule 2). Key rotation is enabled by default.
 */
export class Encryption extends Construct {
  public readonly serviceKey: Key;

  constructor(scope: Construct, id: string, props: EncryptionProps) {
    super(scope, id);

    const { projectName, removalPolicy } = props;

    this.serviceKey = new Key(this, createConstructId(ResourceName.SERVICE_KEY), {
      alias: createResourceName(projectName, ResourceName.SERVICE_KEY),
      description:
        "Single service CMK for the Custom Connector Framework (tables, bucket, vectors).",
      enableKeyRotation: true,
      removalPolicy,
    });
  }

  /** Grants a principal encrypt + decrypt on the service key. */
  public grantEncryptDecrypt(grantee: IGrantable): Grant | undefined {
    return this.serviceKey.grantEncryptDecrypt(grantee);
  }

  /** Grants a principal decrypt-only on the service key. */
  public grantDecrypt(grantee: IGrantable): Grant | undefined {
    return this.serviceKey.grantDecrypt(grantee);
  }
}
