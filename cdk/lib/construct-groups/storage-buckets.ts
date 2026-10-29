import { RemovalPolicy } from "aws-cdk-lib";
import { IKey } from "aws-cdk-lib/aws-kms";
import { BlockPublicAccess, Bucket, BucketEncryption, ObjectOwnership } from "aws-cdk-lib/aws-s3";
import { Construct } from "constructs";
import { ProjectName, ResourceName, createBucketName, createConstructId } from "../common/config";

/**
 * Configuration for the {@link StorageBuckets} construct group.
 */
export interface StorageBucketsProps {
  /** Project name used to derive globally-unique bucket names. */
  readonly projectName: ProjectName;
  /** Removal policy applied to every bucket on stack deletion. */
  readonly removalPolicy: RemovalPolicy;
  /** Whether resources are retained; drives `autoDeleteObjects`. */
  readonly retainResources: boolean;
  /** AWS account id used to disambiguate bucket names. */
  readonly account: string;
  /** AWS region used to disambiguate bucket names. */
  readonly region: string;
  /** The single service CMK encrypting every bucket. */
  readonly serviceKey: IKey;
  /**
   * Whether to emit S3 `Object Created`/`Object Removed` events to EventBridge
   * on the ingestion bucket. Enabled when a managed knowledge base (Bedrock) or
   * an event-driven ingestion trigger consumes bucket changes.
   */
  readonly ingestionEventBridgeEnabled: boolean;
}

/**
 * The framework's S3 storage: the ingestion bucket connectors write documents
 * (and provider metadata sidecars) into, plus a private access-logs bucket it
 * logs into.
 *
 * Shared baseline on every bucket (secure-infrastructure rules 1-3, 7):
 * block ALL public access, the single service CMK for encryption at rest
 * (never `S3_MANAGED`), a TLS-only bucket policy (`enforceSSL`), versioning,
 * and an explicit removal policy. `bucketKeyEnabled` cuts per-object KMS calls
 * to one data key per bucket.
 *
 * - `ingestionBucket` — the write destination for the default `S3ObjectStore`
 *   and the data source a Bedrock/Quick knowledge base ingests. Optionally
 *   emits EventBridge notifications so an ingestion trigger can react to new
 *   objects.
 * - `accessLogsBucket` — private sink for the ingestion bucket's S3 access
 *   logs; ownership forced to the bucket owner so log delivery works.
 */
export class StorageBuckets extends Construct {
  public readonly ingestionBucket: Bucket;
  public readonly accessLogsBucket: Bucket;

  constructor(scope: Construct, id: string, props: StorageBucketsProps) {
    super(scope, id);

    const {
      projectName,
      removalPolicy,
      retainResources,
      account,
      region,
      serviceKey,
      ingestionEventBridgeEnabled,
    } = props;

    this.accessLogsBucket = new Bucket(this, createConstructId("AccessLogsBucket"), {
      bucketName: createBucketName(projectName, ResourceName.ACCESS_LOGS_BUCKET, account, region),
      encryption: BucketEncryption.KMS,
      encryptionKey: serviceKey,
      bucketKeyEnabled: true,
      blockPublicAccess: BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      versioned: true,
      objectOwnership: ObjectOwnership.BUCKET_OWNER_PREFERRED,
      removalPolicy,
      autoDeleteObjects: !retainResources,
    });

    this.ingestionBucket = new Bucket(this, createConstructId("IngestionBucket"), {
      bucketName: createBucketName(projectName, ResourceName.INGESTION_BUCKET, account, region),
      encryption: BucketEncryption.KMS,
      encryptionKey: serviceKey,
      bucketKeyEnabled: true,
      blockPublicAccess: BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      versioned: true,
      serverAccessLogsBucket: this.accessLogsBucket,
      serverAccessLogsPrefix: "ingestion/",
      eventBridgeEnabled: ingestionEventBridgeEnabled,
      removalPolicy,
      autoDeleteObjects: !retainResources,
    });
  }
}
