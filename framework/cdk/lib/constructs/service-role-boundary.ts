import { Effect, ManagedPolicy, PolicyStatement } from "aws-cdk-lib/aws-iam";
import { Construct } from "constructs";
import { ROLE_PATH, SERVICE_ROLE_BOUNDARY_NAME } from "../common/config";
import { acknowledgeNagRules } from "../common/nag-suppressions";

/**
 * The services the framework's own roles call: the API, workflow, ingestion consumer, Batch
 * execution, and scheduler target roles, and the roles CDK adds for Lambda functions, log
 * delivery, and custom resources. No framework role calls S3 or Bedrock: those go through each
 * tenant's `CcfDestinationRole`. `events` is for the managed rules Step Functions' `.sync`
 * integrations put, such as `StepFunctionsGetEventsForStepFunctionsExecutionRule`.
 */
export const SERVICE_ROLE_SERVICES = [
  "batch",
  "cloudwatch",
  "dynamodb",
  "ecr",
  "events",
  "execute-api",
  "iam",
  "kms",
  "lambda",
  "logs",
  "scheduler",
  "sqs",
  "states",
  "sts",
  "xray",
] as const;

/**
 * `CcfServiceRoleBoundary`, the permissions boundary of every role in the service stack. It
 * allows only the services in {@link SERVICE_ROLE_SERVICES}, so a policy added to any of those
 * roles later can't reach another service. Each role's own policy still scopes its actions and
 * resources.
 */
export class ServiceRoleBoundary extends Construct {
  public readonly policy: ManagedPolicy;

  constructor(scope: Construct, id: string) {
    super(scope, id);
    this.policy = new ManagedPolicy(this, "Policy", {
      managedPolicyName: SERVICE_ROLE_BOUNDARY_NAME,
      path: ROLE_PATH,
      description:
        "Permissions boundary of the framework's service roles: only the services they call.",
      statements: [
        new PolicyStatement({
          sid: "ServicesTheFrameworkCalls",
          effect: Effect.ALLOW,
          actions: SERVICE_ROLE_SERVICES.map((service) => `${service}:*`),
          resources: ["*"],
        }),
      ],
    });
    acknowledgeNagRules({
      scopes: [this.policy],
      acknowledgments: [
        {
          id: "AwsSolutions-IAM5",
          reason:
            "A permissions boundary caps the services a role can call; each role's own policy scopes the " +
            "actions and resources.",
          findings: [
            ...SERVICE_ROLE_SERVICES.map((service) => `Action::${service}:*`),
            "Resource::*",
          ],
        },
      ],
    });
  }
}
