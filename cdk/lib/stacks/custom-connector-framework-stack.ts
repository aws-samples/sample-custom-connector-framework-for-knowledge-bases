import { CfnOutput, Stack, StackProps } from "aws-cdk-lib";
import { Construct } from "constructs";
import * as path from "path";
import { CcfServiceReference } from "../common/ccf-service-reference";
import { EnvironmentName, Provider, ProjectName, getRemovalPolicy } from "../common/config";
import { ApiService } from "../construct-groups/api-service";
import { BatchCompute } from "../construct-groups/batch-compute";
import { createBackendImage } from "../construct-groups/compute-image";
import { DatabaseTables } from "../construct-groups/database-tables";
import { DeliveryResources, ServiceAlarms } from "../construct-groups/delivery-reliability";
import { Encryption } from "../construct-groups/encryption";
import { KnowledgeBase } from "../construct-groups/knowledge-base";
import { RuntimeIdentities } from "../construct-groups/runtime-identities";
import { StorageBuckets } from "../construct-groups/storage-buckets";
export interface CustomConnectorFrameworkStackProps extends StackProps {
  readonly projectName: ProjectName;
  readonly environmentName: EnvironmentName;
  readonly retainResources: boolean;
  readonly account: string;
  readonly region: string;
  readonly allowedClientAccountIds: readonly string[];
  readonly provider: Provider;
  readonly embeddingModelId: string;
  readonly vectorDimension: number;
}
/** Thin framework composition root. */
export class CustomConnectorFrameworkStack extends Stack {
  public readonly serviceReference: CcfServiceReference;
  constructor(scope: Construct, id: string, props: CustomConnectorFrameworkStackProps) {
    super(scope, id, props);
    const {
      projectName,
      retainResources,
      account,
      region,
      allowedClientAccountIds,
      provider,
      embeddingModelId,
      vectorDimension,
    } = props;
    const removalPolicy = getRemovalPolicy(retainResources);
    const encryption = new Encryption(this, "Encryption", { projectName, removalPolicy });
    const storage = new StorageBuckets(this, "Storage", {
      projectName,
      removalPolicy,
      retainResources,
      account,
      region,
      serviceKey: encryption.serviceKey,
      ingestionEventBridgeEnabled: provider === Provider.BEDROCK,
    });
    const database = new DatabaseTables(this, "Database", {
      projectName,
      removalPolicy,
      serviceKey: encryption.serviceKey,
    });
    const batch = new BatchCompute(this, "BatchCompute");
    const runtimeIdentities = new RuntimeIdentities(this, "RuntimeIdentities", {
      projectName,
      ingestionBucket: storage.ingestionBucket,
      serviceKey: encryption.serviceKey,
      allowedClientAccountIds,
    });
    const delivery = new DeliveryResources(this, "Delivery", {
      projectName,
      serviceKey: encryption.serviceKey,
      removalPolicy,
    });
    const image = createBackendImage(path.join(__dirname, "..", "..", ".."), removalPolicy);
    const knowledgeBase =
      provider === Provider.BEDROCK
        ? new KnowledgeBase(this, "KnowledgeBase", {
            projectName,
            account,
            region,
            encryption,
            ingestionBucket: storage.ingestionBucket,
            embeddingModelId,
            vectorDimension,
          })
        : undefined;
    const api = new ApiService(this, "ApiService", {
      projectName,
      provider,
      region,
      account,
      allowedClientAccountIds,
      connectorsTable: database.connectorsTable,
      jobsTable: database.jobsTable,
      documentsTable: database.documentsTable,
      configurationKey: encryption.serviceKey,
      jobQueue: batch.jobQueue,
      ingestionBucketName: storage.ingestionBucket.bucketName,
      frameworkExecutionPrincipal: runtimeIdentities.executionPrincipal,
      runtimeRoleBoundary: runtimeIdentities.runtimeRoleBoundary,
      runtimeRoleArnPattern: runtimeIdentities.runtimeRoleArnPattern,
      schedulerGroupName: delivery.schedulerGroup.scheduleGroupName,
      schedulerTargetRole: delivery.schedulerTargetRole,
      schedulerTargetDlq: delivery.schedulerTargetDlq,
      jobsStreamDlq: delivery.jobsStreamDlq,
      batchStatusDlq: delivery.batchStatusDlq,
      bedrockKnowledgeBaseId: knowledgeBase?.knowledgeBase.attrKnowledgeBaseId,
      bedrockDataSourceId: knowledgeBase?.dataSource.attrDataSourceId,
      image,
    });
    new ServiceAlarms(this, "Alarms", {
      schedulerGroupName: delivery.schedulerGroup.scheduleGroupName,
      schedulerTargetDlq: delivery.schedulerTargetDlq,
      jobsStreamDlq: delivery.jobsStreamDlq,
      batchStatusDlq: delivery.batchStatusDlq,
      apiLambda: api.apiLambda,
      scheduledStartLambda: api.scheduledStartLambda,
      jobOrchestratorLambda: api.jobOrchestratorLambda,
      jobStatusLambda: api.jobStatusLambda,
    });
    this.serviceReference = {
      endpoint: api.api.url,
      restApiId: api.api.restApiId,
      region,
      serviceAccountId: account,
      frameworkExecutionPrincipalArn: runtimeIdentities.executionPrincipal.roleArn,
    };
    new CfnOutput(this, "ServiceEndpoint", { value: this.serviceReference.endpoint });
    new CfnOutput(this, "ServiceRestApiId", { value: this.serviceReference.restApiId });
    new CfnOutput(this, "ServiceExecutionPrincipalArn", {
      value: this.serviceReference.frameworkExecutionPrincipalArn,
    });
  }
}
