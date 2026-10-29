import { CfnDataSource, CfnKnowledgeBase } from "aws-cdk-lib/aws-bedrock";
import { PolicyStatement, Role, ServicePrincipal } from "aws-cdk-lib/aws-iam";
import { IBucket } from "aws-cdk-lib/aws-s3";
import { CfnIndex, CfnVectorBucket } from "aws-cdk-lib/aws-s3vectors";
import { NagSuppressions } from "cdk-nag";
import { Construct } from "constructs";
import {
  ProjectName,
  ResourceName,
  createBucketName,
  createConstructId,
  createResourceName,
} from "../common/config";
import { Encryption } from "./encryption";

/**
 * Amazon Titan Text Embeddings V2: 1024-dimension vectors, the standard pairing
 * for a Bedrock Knowledge Base on S3 Vectors. Overridable via context so the
 * choice is not hardcoded and can be re-verified against current availability.
 */
const DEFAULT_EMBEDDING_MODEL_ID = "amazon.titan-embed-text-v2:0";
const DEFAULT_VECTOR_DIMENSION = 1024;
const VECTOR_DATA_TYPE = "float32";
const VECTOR_DISTANCE_METRIC = "cosine";
const S3_VECTORS_STORAGE_TYPE = "S3_VECTORS";
const KMS_SSE_TYPE = "aws:kms";

/**
 * Configuration for the {@link KnowledgeBase} construct group.
 */
export interface KnowledgeBaseProps {
  readonly projectName: ProjectName;
  readonly account: string;
  readonly region: string;
  readonly encryption: Encryption;
  /**
   * The ingestion bucket the KB reads. Connectors land documents plus their
   * `<key>.metadata.json` sidecars here; the KB service role is granted read.
   */
  readonly ingestionBucket: IBucket;
  /** Embedding model id (defaults to Titan Text Embeddings V2). */
  readonly embeddingModelId: string;
  /** Vector dimension; MUST match the embedding model's output dimension. */
  readonly vectorDimension: number;
}

/**
 * A Bedrock Managed Knowledge Base on S3 Vectors that indexes the ingestion
 * bucket, provisioned only when `--context provider=bedrock`. The KB service
 * role is scoped to exactly what ingestion needs: invoke the embedding model,
 * read the ingestion bucket, use the vector store, and decrypt through S3 and
 * S3 Vectors.
 *
 * The toolkit's Bedrock adapter writes a per-document `accessControlList` into
 * each `<key>.metadata.json`. Enforcing those ACLs requires turning on
 * document-level access control for the data source, which the current
 * `CfnDataSource` (aws-cdk-lib's L1 for a managed knowledge base) does not
 * expose. Turn it on with the Bedrock connector API or the console after deploy.
 *
 * Both the Knowledge Base and the S3 Vectors resources are L1 (`CfnKnowledgeBase`,
 * `CfnVectorBucket`, `CfnIndex`): aws-cdk-lib ships no L2 for either, so
 * cdk-best-practices rule 13 selects the L1.
 */
export class KnowledgeBase extends Construct {
  public readonly knowledgeBase: CfnKnowledgeBase;
  public readonly vectorBucket: CfnVectorBucket;
  public readonly vectorIndex: CfnIndex;
  public readonly serviceRole: Role;
  public readonly dataSource: CfnDataSource;

  constructor(scope: Construct, id: string, props: KnowledgeBaseProps) {
    super(scope, id);

    const {
      projectName,
      account,
      region,
      encryption,
      ingestionBucket,
      embeddingModelId,
      vectorDimension,
    } = props;
    const { serviceKey } = encryption;
    const embeddingModelArn = `arn:aws:bedrock:${region}::foundation-model/${embeddingModelId}`;

    this.vectorBucket = new CfnVectorBucket(this, createConstructId(ResourceName.VECTOR_BUCKET), {
      vectorBucketName: createBucketName(projectName, ResourceName.VECTOR_BUCKET, account, region),
      encryptionConfiguration: { sseType: KMS_SSE_TYPE, kmsKeyArn: serviceKey.keyArn },
    });

    this.vectorIndex = new CfnIndex(this, createConstructId(ResourceName.VECTOR_INDEX), {
      indexName: createResourceName(projectName, ResourceName.VECTOR_INDEX).toLowerCase(),
      vectorBucketArn: this.vectorBucket.attrVectorBucketArn,
      dataType: VECTOR_DATA_TYPE,
      dimension: vectorDimension,
      distanceMetric: VECTOR_DISTANCE_METRIC,
      metadataConfiguration: {
        nonFilterableMetadataKeys: ["AMAZON_BEDROCK_TEXT", "AMAZON_BEDROCK_METADATA"],
      },
    });
    this.vectorIndex.addResourceDependency(this.vectorBucket);

    this.serviceRole = new Role(this, createConstructId(ResourceName.KNOWLEDGE_BASE_ROLE), {
      roleName: createResourceName(projectName, ResourceName.KNOWLEDGE_BASE_ROLE),
      assumedBy: new ServicePrincipal("bedrock.amazonaws.com"),
    });
    this.grantIngestion(embeddingModelArn, ingestionBucket, encryption);

    this.knowledgeBase = new CfnKnowledgeBase(
      this,
      createConstructId(ResourceName.KNOWLEDGE_BASE),
      {
        name: createResourceName(projectName, ResourceName.KNOWLEDGE_BASE),
        roleArn: this.serviceRole.roleArn,
        knowledgeBaseConfiguration: {
          type: "VECTOR",
          vectorKnowledgeBaseConfiguration: { embeddingModelArn },
        },
        storageConfiguration: {
          type: S3_VECTORS_STORAGE_TYPE,
          s3VectorsConfiguration: {
            vectorBucketArn: this.vectorBucket.attrVectorBucketArn,
            indexArn: this.vectorIndex.attrIndexArn,
          },
        },
      },
    );
    this.knowledgeBase.addResourceDependency(this.vectorIndex);
    this.knowledgeBase.node.addDependency(this.serviceRole);

    this.dataSource = new CfnDataSource(
      this,
      createConstructId(ResourceName.KNOWLEDGE_BASE_DATA_SOURCE),
      {
        name: createResourceName(projectName, ResourceName.KNOWLEDGE_BASE_DATA_SOURCE),
        knowledgeBaseId: this.knowledgeBase.attrKnowledgeBaseId,
        dataSourceConfiguration: {
          type: "S3",
          s3Configuration: {
            bucketArn: ingestionBucket.bucketArn,
            bucketOwnerAccountId: account,
          },
        },
        serverSideEncryptionConfiguration: { kmsKeyArn: serviceKey.keyArn },
      },
    );
  }

  private grantIngestion(
    embeddingModelArn: string,
    ingestionBucket: IBucket,
    encryption: Encryption,
  ): void {
    this.serviceRole.addToPolicy(
      new PolicyStatement({ actions: ["bedrock:InvokeModel"], resources: [embeddingModelArn] }),
    );
    ingestionBucket.grantRead(this.serviceRole);
    encryption.grantEncryptDecrypt(this.serviceRole);
    this.serviceRole.addToPolicy(
      new PolicyStatement({
        actions: [
          "s3vectors:GetVectorBucket",
          "s3vectors:GetIndex",
          "s3vectors:PutVectors",
          "s3vectors:GetVectors",
          "s3vectors:QueryVectors",
          "s3vectors:ListVectors",
          "s3vectors:DeleteVectors",
        ],
        resources: [this.vectorBucket.attrVectorBucketArn, this.vectorIndex.attrIndexArn],
      }),
    );
    NagSuppressions.addResourceSuppressions(
      this.serviceRole,
      [
        {
          id: "AwsSolutions-IAM5",
          reason:
            "grantRead on the single ingestion bucket the KB ingests; the read-verb and object-scope " +
            "wildcards are CDK grant expansions already scoped to that one bucket ARN.",
          appliesTo: [
            "Action::s3:GetObject*",
            "Action::s3:GetBucket*",
            "Action::s3:List*",
            { regex: "/^Resource::<.*IngestionBucket.*\\.Arn>/\\*$/" },
          ],
        },
        {
          id: "AwsSolutions-IAM5",
          reason:
            "grantEncryptDecrypt expands kms:GenerateDataKey*/kms:ReEncrypt* action-name wildcards, " +
            "scoped to the single service CMK.",
          appliesTo: ["Action::kms:GenerateDataKey*", "Action::kms:ReEncrypt*"],
        },
      ],
      true,
    );
  }

  public static resolveEmbeddingModelId(embeddingModelId: string | undefined): string {
    return embeddingModelId ?? DEFAULT_EMBEDDING_MODEL_ID;
  }

  public static resolveVectorDimension(vectorDimension: string | undefined): number {
    return vectorDimension === undefined ? DEFAULT_VECTOR_DIMENSION : Number(vectorDimension);
  }
}
