import { Duration, RemovalPolicy } from "aws-cdk-lib";
import { IKey } from "aws-cdk-lib/aws-kms";
import { DeadLetterQueue, Queue, QueueEncryption } from "aws-cdk-lib/aws-sqs";
import { Construct } from "constructs";
import { ProjectName, ResourceName, createResourceName } from "../common/config";

export interface ServiceQueueProps {
  readonly projectName: ProjectName;
  /** The queue's fixed name is `<project><name>`, which the Ops stack's alarms and other accounts' policies name. */
  readonly name: ResourceName;
  readonly serviceKey: IKey;
  readonly retention: Duration;
  readonly removalPolicy: RemovalPolicy;
  readonly visibilityTimeout?: Duration;
  readonly deadLetterQueue?: DeadLetterQueue;
}

/** One framework queue: encrypted with the service key, TLS only, with the configured retention. */
export class ServiceQueue extends Construct {
  public readonly queue: Queue;

  constructor(scope: Construct, id: string, props: ServiceQueueProps) {
    super(scope, id);
    this.queue = new Queue(this, "Queue", {
      queueName: createResourceName({ projectName: props.projectName, resourceName: props.name }),
      encryption: QueueEncryption.KMS,
      encryptionMasterKey: props.serviceKey,
      enforceSSL: true,
      retentionPeriod: props.retention,
      removalPolicy: props.removalPolicy,
      visibilityTimeout: props.visibilityTimeout,
      deadLetterQueue: props.deadLetterQueue,
    });
  }
}
