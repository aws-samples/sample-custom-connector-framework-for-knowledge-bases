import { Duration, RemovalPolicy } from "aws-cdk-lib";
import { Role, ServicePrincipal } from "aws-cdk-lib/aws-iam";
import { IKey } from "aws-cdk-lib/aws-kms";
import { ScheduleGroup } from "aws-cdk-lib/aws-scheduler";
import { Queue } from "aws-cdk-lib/aws-sqs";
import { Construct } from "constructs";
import { ProjectName, ResourceName, createResourceName } from "../common/config";
import { ServiceQueue } from "../constructs/service-queue";

export interface DeliveryResourcesProps {
  readonly projectName: ProjectName;
  readonly serviceKey: IKey;
  /** How long each dead-letter queue keeps a message. */
  readonly retention: Duration;
  readonly removalPolicy: RemovalPolicy;
}

/** The scheduler's dead-letter queue, and the Scheduler group and target role every schedule uses. */
export class DeliveryResources extends Construct {
  public readonly schedulerGroup: ScheduleGroup;
  public readonly schedulerTargetRole: Role;
  public readonly schedulerTargetDlq: Queue;

  constructor(scope: Construct, id: string, props: DeliveryResourcesProps) {
    super(scope, id);
    const { projectName, serviceKey, retention, removalPolicy } = props;
    const name = ResourceName.SCHEDULER_TARGET_DLQ;
    this.schedulerTargetDlq = new ServiceQueue(this, name, {
      projectName,
      name,
      serviceKey,
      retention,
      removalPolicy,
    }).queue;
    this.schedulerGroup = new ScheduleGroup(this, ResourceName.SCHEDULER_GROUP, {
      scheduleGroupName: createResourceName({
        projectName,
        resourceName: ResourceName.SCHEDULER_GROUP,
      }),
      removalPolicy,
    });
    this.schedulerTargetRole = new Role(this, ResourceName.SCHEDULER_TARGET_ROLE, {
      roleName: createResourceName({
        projectName,
        resourceName: ResourceName.SCHEDULER_TARGET_ROLE,
      }),
      assumedBy: new ServicePrincipal("scheduler.amazonaws.com"),
      description: "Invokes the one framework scheduled-start target.",
    });
    this.schedulerTargetDlq.grantSendMessages(this.schedulerTargetRole);
  }
}
