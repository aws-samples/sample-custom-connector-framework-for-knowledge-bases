import { Validations } from "aws-cdk-lib";
import { Topic } from "aws-cdk-lib/aws-sns";
import { Construct } from "constructs";

/** The SNS topic one Ops stack's alarms notify on ALARM and on OK. It accepts only TLS publishes. */
export class AlarmTopic extends Construct {
  public readonly topic: Topic;

  constructor(scope: Construct, id: string) {
    super(scope, id);
    this.topic = new Topic(this, "Topic", { enforceSSL: true });
    Validations.of(this.topic).acknowledge({
      id: "AwsSolutions-SNS2",
      reason:
        "CloudWatch alarms cannot publish to a topic encrypted with the AWS managed SNS key, and these " +
        "stacks use service-managed keys for alarm traffic. The topic carries alarm state only.",
    });
  }
}
