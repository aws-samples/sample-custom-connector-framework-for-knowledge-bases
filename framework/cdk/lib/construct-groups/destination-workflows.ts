import { ArnFormat, Aws, RemovalPolicy, Stack, Token } from "aws-cdk-lib";
import { ITable } from "aws-cdk-lib/aws-dynamodb";
import { Effect, PolicyStatement, Role, ServicePrincipal } from "aws-cdk-lib/aws-iam";
import { IKey } from "aws-cdk-lib/aws-kms";
import { RetentionDays } from "aws-cdk-lib/aws-logs";
import { StateMachine } from "aws-cdk-lib/aws-stepfunctions";
import { Construct } from "constructs";
import { DestinationWorkflowSettings, ROLE_PATH } from "../common/config";
import { metricsContract } from "../common/contracts";
import { DestinationWorkflowName, destinationRoleArns } from "../common/destination-names";
import {
  acknowledgeNagRules,
  assumeDestinationRolesAcknowledgment,
  resourceFindings,
  WORKFLOW_ROLE_STAR_ACKNOWLEDGMENT,
} from "../common/nag-suppressions";
import { destinationCreationStates } from "./destination-creation-states";
import { destinationDeletionStates } from "./destination-deletion-states";
import { jsonataStateMachine } from "./workflow-states";

export interface DestinationWorkflowsProps {
  /** `CcfDestinations`, whose items both workflows update. */
  readonly destinationsTable: ITable;
  /** The service key that encrypts the tables. */
  readonly serviceKey: IKey;
  /** The service account; its `CcfDestinationRole`s are assumable too, for the same-account walkthrough. */
  readonly serviceAccountId: string;
  /** The tenant accounts whose `CcfDestinationRole`s the workflows assume. */
  readonly allowedTenantAccountIds: readonly string[];
  /** The service Region, known at synth: it decides `CreateBucket`'s location constraint. */
  readonly region: string;
  /** The data source's deletion protection threshold, 0 to 100. */
  readonly deletionProtectionThreshold: number;
  readonly settings: DestinationWorkflowSettings;
  readonly logRetention: RetentionDays;
  readonly removalPolicy: RemovalPolicy;
}

const stateMachineArn = (name: string): string =>
  `arn:${Aws.PARTITION}:states:${Aws.REGION}:${Aws.ACCOUNT_ID}:stateMachine:${name}`;

/**
 * `CcfDestinationWorkflow`, which the API starts for both actions, and `CcfDestinationDeletionWorkflow`,
 * which it runs for a delete and a quota cleanup. Both run as `CcfDestinationWorkflowRole`, which
 * reaches tenant resources only through each tenant's `CcfDestinationRole` and holds no S3, IAM,
 * or Bedrock permission of its own.
 */
export class DestinationWorkflows extends Construct {
  public readonly role: Role;
  public readonly creation: StateMachine;
  public readonly deletion: StateMachine;

  constructor(scope: Construct, id: string, props: DestinationWorkflowsProps) {
    const {
      destinationsTable,
      serviceKey,
      region,
      deletionProtectionThreshold,
      settings,
      logRetention,
      removalPolicy,
    } = props;
    super(scope, id);
    if (Token.isUnresolved(region))
      throw new Error(
        "DestinationWorkflows needs the service Region at synth to write CreateBucket's location.",
      );
    this.role = new Role(this, "Role", {
      roleName: DestinationWorkflowName.ROLE,
      path: ROLE_PATH,
      assumedBy: new ServicePrincipal("states.amazonaws.com").withConditions({
        StringEquals: { "aws:SourceAccount": Aws.ACCOUNT_ID },
        ArnLike: {
          "aws:SourceArn": [
            stateMachineArn(DestinationWorkflowName.STATE_MACHINE),
            stateMachineArn(DestinationWorkflowName.DELETION_STATE_MACHINE),
          ],
        },
      }),
      description:
        "Runs both destination workflows; reaches tenant resources only through CcfDestinationRole.",
    });
    serviceKey.grant(this.role, "kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey");
    // PublishCreationFailed's task can't put a condition on the statement it generates.
    this.role.addToPolicy(
      new PolicyStatement({
        sid: "MetricsOnlyInTheFrameworkNamespace",
        effect: Effect.DENY,
        actions: ["cloudwatch:PutMetricData"],
        resources: ["*"],
        conditions: { StringNotEquals: { "cloudwatch:namespace": metricsContract().namespace } },
      }),
    );
    const roleArns = destinationRoleArns(props);
    const machine = { role: this.role, logRetention, removalPolicy };
    this.deletion = jsonataStateMachine(this, "Deletion", {
      ...machine,
      stateMachineName: DestinationWorkflowName.DELETION_STATE_MACHINE,
      definition: destinationDeletionStates(new Construct(this, "DeletionStates"), {
        destinationsTable,
        destinationRoleArns: roleArns,
        settings,
      }),
    });
    this.creation = jsonataStateMachine(this, "Creation", {
      ...machine,
      stateMachineName: DestinationWorkflowName.STATE_MACHINE,
      definition: destinationCreationStates(new Construct(this, "CreationStates"), {
        destinationsTable,
        destinationRoleArns: roleArns,
        // By name, not by reference: the shared role's policy names the child, and the child depends on that policy.
        deletionStateMachine: StateMachine.fromStateMachineName(
          this,
          "DeletionByName",
          DestinationWorkflowName.DELETION_STATE_MACHINE,
        ),
        region,
        deletionProtectionThreshold,
        ingestionLogRetention: logRetention,
        settings,
      }),
    });
    this.creation.node.addDependency(this.deletion);
    acknowledgeNagRules({
      scopes: [this.role],
      acknowledgments: [
        WORKFLOW_ROLE_STAR_ACKNOWLEDGMENT,
        assumeDestinationRolesAcknowledgment({ scope: this, resources: roleArns }),
        {
          id: "AwsSolutions-IAM5",
          reason:
            "The .sync run of the deletion workflow describes and stops the executions it starts, which " +
            "are named per request, so the grant names CcfDestinationDeletionWorkflow's executions.",
          findings: resourceFindings({
            scope: this,
            resources: [
              Stack.of(this).formatArn({
                service: "states",
                resource: "execution",
                resourceName: `${DestinationWorkflowName.DELETION_STATE_MACHINE}*`,
                arnFormat: ArnFormat.COLON_RESOURCE_NAME,
              }),
            ],
          }),
        },
      ],
    });
  }
}
