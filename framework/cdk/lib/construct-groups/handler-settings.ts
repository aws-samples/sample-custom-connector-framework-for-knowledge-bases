import { Aws } from "aws-cdk-lib";
import { IKey } from "aws-cdk-lib/aws-kms";
import { DatabaseTables } from "./database-tables";

export interface HandlerSettingsRequest {
  readonly tables: DatabaseTables;
  /** The service key, which encrypts connector configurations, job configurations, and checkpoints. */
  readonly serviceKey: IKey;
  readonly serviceAccountId: string;
}

/**
 * The environment every backend Lambda reads into `HandlerSettings`: the partition, the service
 * account, the resource data key, and the six tables. A double underscore separates a settings
 * group from its member, so `TABLES__DESTINATIONS` is `tables.destinations`. Lambda sets `AWS_REGION`.
 */
export const handlerSettingsEnvironment = ({
  tables,
  serviceKey,
  serviceAccountId,
}: HandlerSettingsRequest): Record<string, string> => ({
  AWS_PARTITION: Aws.PARTITION,
  SERVICE_ACCOUNT_ID: serviceAccountId,
  RESOURCE_DATA_KEY_ARN: serviceKey.keyArn,
  TABLES__DESTINATIONS: tables.destinationsTable.tableName,
  TABLES__CONNECTORS: tables.connectorsTable.tableName,
  TABLES__JOBS: tables.jobsTable.tableName,
  TABLES__COLLECTIONS: tables.collectionsTable.tableName,
  TABLES__DOCUMENTS: tables.documentsTable.tableName,
  TABLES__IDEMPOTENCY: tables.idempotencyTable.tableName,
});
