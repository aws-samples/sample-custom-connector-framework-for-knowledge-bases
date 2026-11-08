import { Aws, RemovalPolicy } from "aws-cdk-lib";
import { ITable } from "aws-cdk-lib/aws-dynamodb";
import { IGrantable, Role, ServicePrincipal } from "aws-cdk-lib/aws-iam";
import { RetentionDays } from "aws-cdk-lib/aws-logs";
import { StateMachine } from "aws-cdk-lib/aws-stepfunctions";
import { Construct } from "constructs";
import { ConnectorDeletionWorkflowName, JobExecutionName, ROLE_PATH } from "../common/config";
import { destinationRoleArns } from "../common/destination-names";
import {
  acknowledgeNagRules,
  assumeDestinationRolesAcknowledgment,
  WORKFLOW_ROLE_STAR_ACKNOWLEDGMENT,
} from "../common/nag-suppressions";
import { ConnectorDeletionIngestion, connectorDeletionStates } from "./connector-deletion-states";
import { RuntimeRoleName } from "./runtime-identities";
import { jsonataStateMachine } from "./workflow-states";

/** The ingestion request builder and its grant, as `IngestionQueue` offers them. */
export interface ConnectorDeletionIngestionQueue extends ConnectorDeletionIngestion {
  grantRequestIngestion(grantee: IGrantable): void;
}

export interface ConnectorDeletionWorkflowProps {
  readonly documentsTable: ITable;
  readonly collectionsTable: ITable;
  readonly connectorsTable: ITable;
  readonly ingestion: ConnectorDeletionIngestionQueue;
  readonly scheduleGroupName: string;
  readonly runtimeRoleArnPattern: string;
  readonly serviceAccountId: string;
  readonly allowedTenantAccountIds: readonly string[];
  readonly logRetention: RetentionDays;
  readonly removalPolicy: RemovalPolicy;
}

/**
 * `CcfConnectorDeletionWorkflow` and its role. The workflow has no catch other than not found: a
 * failed execution leaves the custom connector `DELETING`, and a new `DeleteCustomConnector`
 * starts it again. Each task's statement comes from its own call, so the role holds no other.
 */
export class ConnectorDeletionWorkflow extends Construct {
  public readonly stateMachine: StateMachine;
  public readonly role: Role;

  constructor(scope: Construct, id: string, props: ConnectorDeletionWorkflowProps) {
    super(scope, id);
    const {
      ingestion,
      logRetention,
      removalPolicy,
      serviceAccountId,
      allowedTenantAccountIds,
      scheduleGroupName,
    } = props;
    this.role = new Role(this, "Role", {
      roleName: ConnectorDeletionWorkflowName.ROLE,
      path: ROLE_PATH,
      assumedBy: new ServicePrincipal("states.amazonaws.com").withConditions({
        StringEquals: { "aws:SourceAccount": Aws.ACCOUNT_ID },
        ArnLike: {
          "aws:SourceArn": `arn:${Aws.PARTITION}:states:${Aws.REGION}:${Aws.ACCOUNT_ID}:stateMachine:${ConnectorDeletionWorkflowName.STATE_MACHINE}`,
        },
      }),
      description:
        "Runs CcfConnectorDeletionWorkflow; reaches tenant buckets only through CcfDestinationRole.",
    });
    // The service key encrypts the tables the workflow reads and writes, and the ingestion queue.
    ingestion.grantRequestIngestion(this.role);
    this.stateMachine = jsonataStateMachine(this, "StateMachine", {
      stateMachineName: ConnectorDeletionWorkflowName.STATE_MACHINE,
      definition: connectorDeletionStates(this, {
        ...props,
        destinationRoleArns: destinationRoleArns({ serviceAccountId, allowedTenantAccountIds }),
      }),
      role: this.role,
      logRetention,
      removalPolicy,
    });
    acknowledgeNagRules({
      scopes: [this.role],
      acknowledgments: [
        WORKFLOW_ROLE_STAR_ACKNOWLEDGMENT,
        assumeDestinationRolesAcknowledgment({
          scope: this,
          resources: destinationRoleArns({ serviceAccountId, allowedTenantAccountIds }),
        }),
        {
          id: "AwsSolutions-IAM5",
          reason:
            "Runtime roles (their path and prefix), schedules (schedule/<framework group>/*), and job definitions " +
            "(the framework's prefix, any revision) are created at run time per custom connector, so their names " +
            "aren't known at synth; the prefix and the group bound them.",
          findings: [
            `Resource::arn:<AWS::Partition>:iam::<AWS::AccountId>:role${RuntimeRoleName.PATH}${RuntimeRoleName.NAME_PREFIX}*`,
            `Resource::arn:<AWS::Partition>:scheduler:<AWS::Region>:<AWS::AccountId>:schedule/${scheduleGroupName}/*`,
            `Resource::arn:<AWS::Partition>:batch:<AWS::Region>:<AWS::AccountId>:job-definition/${JobExecutionName.DEFINITION_PREFIX}*:*`,
          ],
        },
      ],
    });
  }
}
