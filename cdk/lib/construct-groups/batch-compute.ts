import { Duration } from "aws-cdk-lib";
import { FargateComputeEnvironment, JobQueue } from "aws-cdk-lib/aws-batch";
import {
  FlowLogDestination,
  FlowLogTrafficType,
  GatewayVpcEndpointAwsService,
  SubnetType,
  Vpc,
} from "aws-cdk-lib/aws-ec2";
import { Construct } from "constructs";
import { ResourceName, createConstructId } from "../common/config";

/** Fargate compute environment cap; connector jobs are short-lived and low-concurrency. */
const MAX_VCPUS = 256;

/**
 * Configuration for the {@link BatchCompute} construct group.
 */
export interface BatchComputeProps {
  /** Reserved for future per-environment sizing; no fields required today. */
  readonly maxVcpus?: number;
}

/**
 * The AWS Batch infrastructure that runs connector containers on Fargate: a
 * two-AZ VPC with private egress subnets, an S3 gateway endpoint (so document
 * uploads to the ingestion bucket stay off the public internet,
 * secure-infrastructure rule 12), VPC flow logs (rule 11), a Fargate compute
 * environment, and the job queue connector jobs are submitted to.
 *
 * The job orchestrator Lambda submits jobs here; the job queue name is exposed
 * so the API/orchestrator wave can reference it.
 */
export class BatchCompute extends Construct {
  public readonly vpc: Vpc;
  public readonly jobQueue: JobQueue;

  constructor(scope: Construct, id: string, props: BatchComputeProps = {}) {
    super(scope, id);

    const maxVcpus = props.maxVcpus ?? MAX_VCPUS;

    this.vpc = new Vpc(this, createConstructId(ResourceName.VPC), {
      maxAzs: 2,
      natGateways: 1,
      gatewayEndpoints: {
        S3: { service: GatewayVpcEndpointAwsService.S3 },
      },
      flowLogs: {
        FlowLog: {
          destination: FlowLogDestination.toCloudWatchLogs(),
          trafficType: FlowLogTrafficType.ALL,
        },
      },
    });

    const computeEnvironment = new FargateComputeEnvironment(
      this,
      createConstructId(ResourceName.COMPUTE_ENVIRONMENT),
      {
        vpc: this.vpc,
        maxvCpus: maxVcpus,
        updateTimeout: Duration.minutes(30),
        vpcSubnets: { subnetType: SubnetType.PRIVATE_WITH_EGRESS },
      },
    );

    this.jobQueue = new JobQueue(this, createConstructId(ResourceName.JOB_QUEUE), {
      computeEnvironments: [{ computeEnvironment, order: 1 }],
    });
  }
}
