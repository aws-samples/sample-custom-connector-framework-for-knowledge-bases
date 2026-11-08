import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { Duration, RemovalPolicy, Stack } from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { Role, ServicePrincipal } from "aws-cdk-lib/aws-iam";
import { RetentionDays } from "aws-cdk-lib/aws-logs";
import { Succeed } from "aws-cdk-lib/aws-stepfunctions";
import { IngestionQueueSettings, ProjectName, SERVICE_SETTINGS } from "../lib/common/config";
import { createBackendBundle } from "../lib/construct-groups/backend-bundle";
import { DatabaseTables } from "../lib/construct-groups/database-tables";
import { IngestionQueue } from "../lib/construct-groups/ingestion-queue";
import { jsonataStateMachine } from "../lib/construct-groups/workflow-states";
import { ServiceKey } from "../lib/constructs/service-key";
import {
  IMAGE_DIRECTORY,
  JsonObject,
  SERVICE_ACCOUNT,
  createApp,
  propertiesOf,
  resourcesOf,
} from "./construct-fixtures";
import { renderedDefinition, roleStatements, structuralProblems } from "./state-machine-definition";

const SETTINGS: IngestionQueueSettings = {
  retryDelaysSeconds: [30, 90],
  maxReceiveCount: 7,
  consumerReservedConcurrency: 3,
};
const MESSAGE_SCHEMA = JSON.parse(
  readFileSync(
    resolve(__dirname, "..", "..", "backend", "generated", "ingestion-request-message.schema.json"),
    "utf8",
  ),
) as {
  readonly required: readonly string[];
  readonly properties: { readonly token: { readonly pattern: string } };
};

interface Built {
  readonly stack: Stack;
  readonly ingestion: IngestionQueue;
}

const build = (settings: IngestionQueueSettings = SETTINGS): Built => {
  const stack = new Stack(createApp(), "Service", {
    env: { account: SERVICE_ACCOUNT, region: "us-east-1" },
  });
  const removalPolicy = RemovalPolicy.DESTROY;
  const serviceKey = new ServiceKey(stack, "Encryption", {
    projectName: ProjectName.CCF,
    removalPolicy,
  }).key;
  const ingestion = new IngestionQueue(stack, "Ingestion", {
    tables: new DatabaseTables(stack, "Database", { removalPolicy, serviceKey }),
    serviceKey,
    serviceAccountId: SERVICE_ACCOUNT,
    allowedTenantAccountIds: [],
    destinationWorkflowArn: `arn:aws:states:us-east-1:${SERVICE_ACCOUNT}:stateMachine:CcfDestinationWorkflow`,
    connectorDeletionWorkflowArn: `arn:aws:states:us-east-1:${SERVICE_ACCOUNT}:stateMachine:CcfConnectorDeletionWorkflow`,
    bundle: createBackendBundle({ sourcePath: IMAGE_DIRECTORY }),
    memorySize: 1024,
    timeout: Duration.seconds(29),
    retention: Duration.days(14),
    settings,
    logRetention: RetentionDays.ONE_WEEK,
    removalPolicy,
  });
  return { stack, ingestion };
};

/** A state machine that requests an ingestion once per prefix, and its rendered definition. */
const requester = (prefixes: readonly string[]): ReturnType<typeof renderedDefinition> => {
  const { stack, ingestion } = build();
  const done = Succeed.jsonata(stack, "Done");
  const definition = prefixes.reduceRight<ReturnType<IngestionQueue["requestIngestion"]>>(
    (next, statePrefix) =>
      ingestion.requestIngestion(stack, {
        statePrefix,
        tenantArnPrefix: "$states.context.Execution.Input.tenant_arn_prefix",
        destinationId: "$states.context.Execution.Input.destination_id",
        next,
      }),
    done,
  );
  const role = new Role(stack, "Role", { assumedBy: new ServicePrincipal("states.amazonaws.com") });
  jsonataStateMachine(stack, "Requester", {
    stateMachineName: "Requester",
    definition,
    role,
    logRetention: RetentionDays.ONE_WEEK,
    removalPolicy: RemovalPolicy.DESTROY,
  });
  return renderedDefinition(Template.fromStack(stack), "Requester");
};

describe("IngestionQueue", () => {
  const template = Template.fromStack(build().stack);

  it("redrives a message to the dead-letter queue after the configured receives", () => {
    const [queue] = resourcesOf(template, "AWS::SQS::Queue").filter(
      (resource) => propertiesOf(resource).QueueName === "CcfIngestionQueue",
    );
    expect(propertiesOf(queue).RedrivePolicy).toMatchObject({ maxReceiveCount: 7 });
  });

  it("gives the consumer the configured reserved concurrency", () => {
    template.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: "CcfIngestionConsumerLambda",
      ReservedConcurrentExecutions: 3,
    });
  });

  it("fails synthesis, naming the setting, when the retries outlast the retention", () => {
    expect(() => build({ ...SETTINGS, retryDelaysSeconds: [86_400], maxReceiveCount: 15 })).toThrow(
      /SERVICE_SETTINGS\.ingestionQueue\.retryDelaysSeconds/,
    );
  });

  it("accepts today's settings", () => {
    expect(() => build(SERVICE_SETTINGS.ingestionQueue)).not.toThrow();
  });

  it("grants the consumer each SQS consume action once", () => {
    const actions = roleStatements(template, "CcfIngestionConsumerRole")
      .flatMap((statement) => statement.Action)
      .filter((action) => String(action).startsWith("sqs:") && action !== "sqs:SendMessage");
    expect([...actions].sort()).toEqual([...new Set(actions)].sort());
    expect(actions).toEqual(expect.arrayContaining(["sqs:ReceiveMessage", "sqs:DeleteMessage"]));
  });
});

describe("IngestionQueue.requestIngestion", () => {
  const definition = requester(["Job"]);
  const update = definition.States.JobSetIngestionPending;
  const send = definition.States.JobSendIngestionMessage;

  it("names both states with the prefix and continues to next on a failed condition", () => {
    expect(update.Next).toBe("JobSendIngestionMessage");
    expect(send.Next).toBe("Done");
    expect(update.Catch).toEqual([
      expect.objectContaining({
        ErrorEquals: ["DynamoDb.ConditionalCheckFailedException"],
        Next: "Done",
      }),
    ]);
  });

  it("writes only the token, and only for an existing BEDROCK destination", () => {
    const argumentsOf = update.Arguments as JsonObject;
    expect(argumentsOf.UpdateExpression).not.toContain("updated_at");
    expect(argumentsOf.ConditionExpression).toBe(
      "attribute_exists(destination_id) AND #type = :bedrock",
    );
    expect(argumentsOf.ExpressionAttributeValues).toMatchObject({ ":bedrock": { S: "BEDROCK" } });
  });

  it("sends exactly the message members the consumer's schema requires", () => {
    const body = String((send.Arguments as JsonObject).MessageBody);
    const members = [...body.matchAll(/'([a-z_]+)':/g)].map((match) => match[1]);
    expect(members.sort()).toEqual([...MESSAGE_SCHEMA.required].sort());
  });

  it("makes tokens in the format the consumer's schema accepts", () => {
    const tokenExpression = JSON.stringify(update.Arguments);
    expect(tokenExpression).toContain("'ing-' & $uuid()");
    expect(`ing-${crypto.randomUUID()}`).toMatch(
      new RegExp(MESSAGE_SCHEMA.properties.token.pattern),
    );
  });

  it("can run twice in one state machine with two prefixes", () => {
    expect(structuralProblems(requester(["First", "Second"]))).toEqual([]);
  });
});
