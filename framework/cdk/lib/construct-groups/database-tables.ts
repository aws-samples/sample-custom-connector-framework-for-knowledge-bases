import { RemovalPolicy } from "aws-cdk-lib";
import {
  AttributeType,
  BillingMode,
  ProjectionType,
  Table,
  TableEncryption,
} from "aws-cdk-lib/aws-dynamodb";
import { IKey } from "aws-cdk-lib/aws-kms";
import { Construct } from "constructs";
import {
  KeyAttribute,
  TableContract,
  TablesContract,
  summaryAttributes,
  tablesContract,
} from "../common/contracts";

const ATTRIBUTE_TYPES = {
  STRING: AttributeType.STRING,
  NUMBER: AttributeType.NUMBER,
} satisfies Record<KeyAttribute["type"], AttributeType>;

export interface DatabaseTablesProps {
  /** Applied to every table on stack deletion. */
  readonly removalPolicy: RemovalPolicy;
  /** The service key that encrypts every table. */
  readonly serviceKey: IKey;
}

const attribute = (key: KeyAttribute): { name: string; type: AttributeType } => ({
  name: key.name,
  type: ATTRIBUTE_TYPES[key.type],
});

/**
 * The framework's six DynamoDB tables, keyed, indexed, and expired as `tables.json` says, which the
 * backend's DAOs and test tables read too. Every table is on demand, encrypted with the service key,
 * and has point-in-time recovery; each index projects its summary schema's members.
 */
export class DatabaseTables extends Construct {
  public readonly destinationsTable: Table;
  public readonly connectorsTable: Table;
  public readonly jobsTable: Table;
  public readonly collectionsTable: Table;
  public readonly documentsTable: Table;
  public readonly idempotencyTable: Table;
  /** `<table ARN>/index/*` for every table, which a CDK table grant names. */
  public readonly indexesArns: readonly string[];

  constructor(scope: Construct, id: string, props: DatabaseTablesProps) {
    super(scope, id);
    const tables: TablesContract = tablesContract();
    const table = (contract: TableContract): Table => {
      const created = new Table(this, contract.name, {
        tableName: contract.name,
        billingMode: BillingMode.PAY_PER_REQUEST,
        encryption: TableEncryption.CUSTOMER_MANAGED,
        encryptionKey: props.serviceKey,
        pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
        removalPolicy: props.removalPolicy,
        partitionKey: attribute(contract.partitionKey),
        ...(contract.sortKey === null ? {} : { sortKey: attribute(contract.sortKey) }),
        ...(contract.timeToLiveAttribute === null
          ? {}
          : { timeToLiveAttribute: contract.timeToLiveAttribute }),
      });
      const tableKeys = [contract.partitionKey, contract.sortKey];
      for (const [indexName, index] of Object.entries(contract.indexes)) {
        const keys = [...tableKeys, index.partitionKey, index.sortKey]
          .filter((key): key is KeyAttribute => key !== null)
          .map((key) => key.name);
        created.addGlobalSecondaryIndex({
          indexName,
          partitionKey: attribute(index.partitionKey),
          ...(index.sortKey === null ? {} : { sortKey: attribute(index.sortKey) }),
          projectionType: ProjectionType.INCLUDE,
          nonKeyAttributes: [...summaryAttributes({ schema: index.projection, keys })],
        });
      }
      return created;
    };
    this.destinationsTable = table(tables.destinations);
    this.connectorsTable = table(tables.connectors);
    this.jobsTable = table(tables.jobs);
    this.collectionsTable = table(tables.collections);
    this.documentsTable = table(tables.documents);
    this.idempotencyTable = table(tables.idempotency);
    this.indexesArns = [
      this.destinationsTable,
      this.connectorsTable,
      this.jobsTable,
      this.collectionsTable,
      this.documentsTable,
      this.idempotencyTable,
    ].map((created) => `${created.tableArn}/index/*`);
  }

  /** The six tables, which the API Lambda reads and writes. */
  public get all(): readonly Table[] {
    return [
      this.destinationsTable,
      this.connectorsTable,
      this.jobsTable,
      this.collectionsTable,
      this.documentsTable,
      this.idempotencyTable,
    ];
  }
}
