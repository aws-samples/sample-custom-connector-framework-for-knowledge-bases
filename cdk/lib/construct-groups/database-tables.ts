import { RemovalPolicy } from "aws-cdk-lib";
import {
  AttributeType,
  BillingMode,
  ProjectionType,
  StreamViewType,
  Table,
  TableEncryption,
} from "aws-cdk-lib/aws-dynamodb";
import { IKey } from "aws-cdk-lib/aws-kms";
import { Construct } from "constructs";
import { ProjectName, ResourceName, createConstructId, createResourceName } from "../common/config";

/** The GSI (on the jobs and documents tables) that lists a connector's rows. */
const BY_CONNECTOR_INDEX = "GSI1";
const ARN_PREFIX_KEY = "custom_connector_arn_prefix";
const CONNECTOR_ID_KEY = "connector_id";

/**
 * Configuration for the {@link DatabaseTables} construct group.
 */
export interface DatabaseTablesProps {
  /** Project name used to derive table names. */
  readonly projectName: ProjectName;
  /** Removal policy applied to every table on stack deletion. */
  readonly removalPolicy: RemovalPolicy;
  /** The single service CMK encrypting every table. */
  readonly serviceKey: IKey;
}

/**
 * The three DynamoDB tables backing the state service, keyed exactly as the
 * backend DAOs expect:
 *
 * - `connectorsTable` — one row per custom connector (`arn_prefix` + `connector_id`),
 *   also carrying the connector's checkpoint.
 * - `jobsTable` — one row per job (`arn_prefix` + `job_id`); a `NEW_AND_OLD_IMAGES`
 *   stream drives the job orchestrator Lambda.
 * - `documentsTable` — one row per tracked document (`arn_prefix` +
 *   `connector_document_key`) for connector-isolated change detection.
 *
 * Every table uses on-demand billing, SSE-KMS with the single service CMK
 * (secure-infrastructure rule 2), and an explicit removal policy. The jobs and
 * documents tables carry a `GSI1` to list a connector's rows.
 */
export class DatabaseTables extends Construct {
  public readonly connectorsTable: Table;
  public readonly jobsTable: Table;
  public readonly documentsTable: Table;

  constructor(scope: Construct, id: string, props: DatabaseTablesProps) {
    super(scope, id);

    const { projectName, removalPolicy, serviceKey } = props;
    const baseline = {
      billingMode: BillingMode.PAY_PER_REQUEST,
      encryption: TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: serviceKey,
      pointInTimeRecovery: true,
      removalPolicy,
    } as const;

    this.connectorsTable = new Table(this, createConstructId(ResourceName.CONNECTORS_TABLE), {
      ...baseline,
      tableName: createResourceName(projectName, ResourceName.CONNECTORS_TABLE),
      partitionKey: { name: ARN_PREFIX_KEY, type: AttributeType.STRING },
      sortKey: { name: CONNECTOR_ID_KEY, type: AttributeType.STRING },
    });

    this.jobsTable = new Table(this, createConstructId(ResourceName.JOBS_TABLE), {
      ...baseline,
      tableName: createResourceName(projectName, ResourceName.JOBS_TABLE),
      partitionKey: { name: ARN_PREFIX_KEY, type: AttributeType.STRING },
      sortKey: { name: "job_id", type: AttributeType.STRING },
      stream: StreamViewType.NEW_AND_OLD_IMAGES,
    });

    this.documentsTable = new Table(this, createConstructId(ResourceName.DOCUMENTS_TABLE), {
      ...baseline,
      tableName: createResourceName(projectName, ResourceName.DOCUMENTS_TABLE),
      partitionKey: { name: ARN_PREFIX_KEY, type: AttributeType.STRING },
      sortKey: { name: "connector_document_key", type: AttributeType.STRING },
    });

    for (const table of [this.jobsTable, this.documentsTable]) {
      table.addGlobalSecondaryIndex({
        indexName: BY_CONNECTOR_INDEX,
        partitionKey: { name: ARN_PREFIX_KEY, type: AttributeType.STRING },
        sortKey: { name: CONNECTOR_ID_KEY, type: AttributeType.STRING },
        projectionType: ProjectionType.ALL,
      });
    }
  }
}
