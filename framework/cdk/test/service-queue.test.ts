import { Duration, RemovalPolicy, Stack } from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import { Key } from "aws-cdk-lib/aws-kms";
import { ProjectName, ResourceName } from "../lib/common/config";
import { ServiceQueue } from "../lib/constructs/service-queue";
import { SERVICE_ACCOUNT, createApp, onlyResourceId } from "./construct-fixtures";

const stack = new Stack(createApp(), "Queues", { env: { account: SERVICE_ACCOUNT } });
const serviceKey = new Key(stack, "ServiceKey");
new ServiceQueue(stack, "Ingestion", {
  projectName: ProjectName.CCF,
  name: ResourceName.INGESTION_DLQ,
  serviceKey,
  retention: Duration.days(14),
  removalPolicy: RemovalPolicy.DESTROY,
});
const template = Template.fromStack(stack);

describe("ServiceQueue", () => {
  it("names the queue after the project and the resource name", () => {
    template.hasResourceProperties("AWS::SQS::Queue", { QueueName: "CcfIngestionDlq" });
  });

  it("encrypts the queue with the service key", () => {
    template.hasResourceProperties("AWS::SQS::Queue", {
      KmsMasterKeyId: { "Fn::GetAtt": [onlyResourceId(template, "AWS::KMS::Key"), "Arn"] },
    });
  });

  it("keeps messages for the given retention", () => {
    template.hasResourceProperties("AWS::SQS::Queue", {
      MessageRetentionPeriod: Duration.days(14).toSeconds(),
    });
  });

  it("denies every request that doesn't use TLS", () => {
    template.hasResourceProperties("AWS::SQS::QueuePolicy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Effect: "Deny",
            Action: "sqs:*",
            Condition: { Bool: { "aws:SecureTransport": "false" } },
          }),
        ]),
      },
    });
  });

  it("follows the given removal policy", () => {
    template.hasResource("AWS::SQS::Queue", { DeletionPolicy: "Delete" });
  });
});
