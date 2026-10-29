import { Stack } from "aws-cdk-lib";
import { NagSuppressions } from "cdk-nag";
/** Apply only the cdk-nag findings produced by the sample registration path. */
export function applySampleNagSuppressions(stack: Stack): void {
  NagSuppressions.addResourceSuppressionsByPath(
    stack,
    `${stack.node.path}/ConnectorRegistrationFn/Resource`,
    [
      {
        id: "AwsSolutions-L1",
        reason:
          "The registration handler uses Python 3.14 to match its hash-locked runtime dependencies.",
      },
    ],
  );
  for (const pathSuffix of [
    "ConnectorRegistrationFn/ServiceRole/Resource",
    "ConnectorRegistrationProvider/framework-onEvent/ServiceRole/Resource",
  ]) {
    NagSuppressions.addResourceSuppressionsByPath(stack, `${stack.node.path}/${pathSuffix}`, [
      {
        id: "AwsSolutions-IAM4",
        reason:
          "CDK Provider roles use AWSLambdaBasicExecutionRole only for standard CloudWatch Logs writes.",
        appliesTo: [
          "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
        ],
      },
    ]);
  }
  NagSuppressions.addResourceSuppressionsByPath(
    stack,
    `${stack.node.path}/ConnectorRegistrationFn/ServiceRole/DefaultPolicy/Resource`,
    [
      {
        id: "AwsSolutions-IAM5",
        reason:
          "Registration is limited to POST on the exact API and PUT/DELETE on the connector id returned at deployment.",
      },
    ],
  );
  NagSuppressions.addResourceSuppressionsByPath(
    stack,
    `${stack.node.path}/ConnectorRegistrationProvider/framework-onEvent/ServiceRole/DefaultPolicy/Resource`,
    [
      {
        id: "AwsSolutions-IAM5",
        reason:
          "The CDK Provider framework role invokes only versions and aliases of ConnectorRegistrationFn.",
      },
    ],
  );
}
