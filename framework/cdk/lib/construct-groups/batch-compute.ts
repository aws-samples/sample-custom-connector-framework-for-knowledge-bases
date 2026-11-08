import { Aws, Duration, RemovalPolicy, Validations } from "aws-cdk-lib";
import { FargateComputeEnvironment, JobQueue } from "aws-cdk-lib/aws-batch";
import {
  FlowLogDestination,
  FlowLogTrafficType,
  GatewayVpcEndpoint,
  GatewayVpcEndpointAwsService,
  AclCidr,
  AclTraffic,
  NetworkAcl,
  SecurityGroup,
  SubnetType,
  TrafficDirection,
  Vpc,
} from "aws-cdk-lib/aws-ec2";
import { Effect, PolicyStatement, StarPrincipal } from "aws-cdk-lib/aws-iam";
import { LogGroup, RetentionDays } from "aws-cdk-lib/aws-logs";
import { Construct } from "constructs";
import {
  DESTINATION_BUCKET_PATTERN,
  JOB_SECURITY_GROUP_NAME,
  ResourceName,
} from "../common/config";

/** The ports a reply to the jobs' outbound connections returns on. */
const EPHEMERAL_PORTS = { startPort: 1024, endPort: 65535 };
/** Network ACL rule numbers; a lower number is evaluated first, and anything no rule allows is denied. */
enum JobSubnetAclRule {
  ALL_OUTBOUND = 100,
  INBOUND_FROM_VPC = 110,
  INBOUND_TCP_REPLIES = 120,
}

export interface BatchComputeProps {
  /** Removal policy for the VPC flow log group. */
  readonly removalPolicy: RemovalPolicy;
  /** Retention of the VPC flow log group. */
  readonly logRetention: RetentionDays;
  /** The Fargate vCPU cap across all running connector jobs. */
  readonly maxVcpus: number;
  readonly availabilityZones: number;
  readonly natGateways: number;
  /** How long CloudFormation waits for a compute environment update. */
  readonly updateTimeout: Duration;
}

/**
 * The AWS Batch compute connector jobs run on: a VPC with NAT gateways and private subnets, VPC
 * flow logs, the S3 gateway endpoint (the VPC's only endpoint) on the private subnets' route
 * tables, `CcfJobSecurityGroup` with no inbound rule, a Fargate compute environment in the private
 * subnets, and the job queue.
 *
 * The endpoint policy lets jobs reach destination buckets in any account and every bucket the
 * service account doesn't own, such as the ECR image layer bucket and a connector's source
 * buckets, so jobs can't reach the framework's own buckets in its Region.
 */
export class BatchCompute extends Construct {
  public readonly vpc: Vpc;
  public readonly jobQueue: JobQueue;
  public readonly jobSecurityGroup: SecurityGroup;
  public readonly s3Endpoint: GatewayVpcEndpoint;
  /** The job subnets' network ACL: all outbound, and inbound only from the VPC or as TCP replies. */
  public readonly jobSubnetAcl: NetworkAcl;

  constructor(scope: Construct, id: string, props: BatchComputeProps) {
    super(scope, id);
    const { removalPolicy, logRetention, maxVcpus, availabilityZones, natGateways, updateTimeout } =
      props;
    const flowLogGroup = new LogGroup(this, "FlowLogs", { retention: logRetention, removalPolicy });
    this.vpc = new Vpc(this, ResourceName.VPC, {
      maxAzs: availabilityZones,
      natGateways,
      // Restricting the default security group needs a CDK custom resource whose Lambda runs on x86_64,
      // which the framework's arm64-only rule rules out; nothing in the VPC uses the default security group.
      restrictDefaultSecurityGroup: false,
      subnetConfiguration: [
        { name: "Public", subnetType: SubnetType.PUBLIC },
        { name: "Private", subnetType: SubnetType.PRIVATE_WITH_EGRESS },
      ],
      flowLogs: {
        FlowLog: {
          destination: FlowLogDestination.toCloudWatchLogs(flowLogGroup),
          trafficType: FlowLogTrafficType.ALL,
        },
      },
    });
    this.s3Endpoint = this.vpc.addGatewayEndpoint("S3Endpoint", {
      service: GatewayVpcEndpointAwsService.S3,
      subnets: [{ subnetType: SubnetType.PRIVATE_WITH_EGRESS }],
    });
    for (const statement of jobEndpointStatements()) this.s3Endpoint.addToPolicy(statement);
    this.jobSubnetAcl = this.createJobSubnetAcl();

    this.jobSecurityGroup = new SecurityGroup(this, "JobSecurityGroup", {
      vpc: this.vpc,
      securityGroupName: JOB_SECURITY_GROUP_NAME,
      description: "Connector jobs: no inbound traffic, all outbound traffic.",
      allowAllOutbound: true,
    });
    const computeEnvironment = new FargateComputeEnvironment(
      this,
      ResourceName.COMPUTE_ENVIRONMENT,
      {
        vpc: this.vpc,
        maxvCpus: maxVcpus,
        updateTimeout,
        vpcSubnets: { subnetType: SubnetType.PRIVATE_WITH_EGRESS },
        securityGroups: [this.jobSecurityGroup],
      },
    );
    this.jobQueue = new JobQueue(this, ResourceName.JOB_QUEUE, {
      computeEnvironments: [{ computeEnvironment, order: 1 }],
    });
  }

  /** Subnet-level defense in depth beside the security group, which already allows no inbound traffic. */
  private createJobSubnetAcl(): NetworkAcl {
    const acl = new NetworkAcl(this, "JobSubnetAcl", {
      vpc: this.vpc,
      subnetSelection: { subnetType: SubnetType.PRIVATE_WITH_EGRESS },
    });
    acl.addEntry("AllOutbound", {
      ruleNumber: JobSubnetAclRule.ALL_OUTBOUND,
      cidr: AclCidr.anyIpv4(),
      traffic: AclTraffic.allTraffic(),
      direction: TrafficDirection.EGRESS,
    });
    acl.addEntry("InboundFromVpc", {
      ruleNumber: JobSubnetAclRule.INBOUND_FROM_VPC,
      cidr: AclCidr.ipv4(this.vpc.vpcCidrBlock),
      traffic: AclTraffic.allTraffic(),
      direction: TrafficDirection.INGRESS,
    });
    acl.addEntry("InboundTcpReplies", {
      ruleNumber: JobSubnetAclRule.INBOUND_TCP_REPLIES,
      cidr: AclCidr.anyIpv4(),
      traffic: AclTraffic.tcpPortRange(EPHEMERAL_PORTS.startPort, EPHEMERAL_PORTS.endPort),
      direction: TrafficDirection.INGRESS,
    });
    Validations.of(acl).acknowledge({
      id: "AwsSolutions-VPC3",
      reason:
        "Subnet-level defense in depth for the job subnets, beside the security group that allows no " +
        "inbound traffic; three rules: all outbound, VPC traffic, and TCP replies on ephemeral ports.",
    });
    return acl;
  }
}

/** The endpoint policy: destination buckets anywhere, and every bucket another account owns. */
const jobEndpointStatements = (): PolicyStatement[] => {
  const bucket = (name: string): string[] => [
    `arn:${Aws.PARTITION}:s3:::${name}`,
    `arn:${Aws.PARTITION}:s3:::${name}/*`,
  ];
  return [
    new PolicyStatement({
      sid: "DestinationBuckets",
      effect: Effect.ALLOW,
      principals: [new StarPrincipal()],
      actions: ["s3:*"],
      resources: bucket(DESTINATION_BUCKET_PATTERN),
    }),
    new PolicyStatement({
      sid: "OtherAccountBuckets",
      effect: Effect.ALLOW,
      principals: [new StarPrincipal()],
      actions: ["s3:*"],
      resources: bucket("*"),
      conditions: { StringNotEquals: { "aws:ResourceAccount": Aws.ACCOUNT_ID } },
    }),
  ];
};
