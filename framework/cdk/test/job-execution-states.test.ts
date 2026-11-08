import { App, Duration, RemovalPolicy, Stack } from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { AttributeType, Table } from "aws-cdk-lib/aws-dynamodb";
import { Role, ServicePrincipal } from "aws-cdk-lib/aws-iam";
import { RetentionDays } from "aws-cdk-lib/aws-logs";
import { Pass } from "aws-cdk-lib/aws-stepfunctions";
import { Construct } from "constructs";
import { JobExecutionName } from "../lib/common/config";
import { RequestIngestionProps } from "../lib/construct-groups/ingestion-queue";
import {
  FAILURE_REASON_EXPRESSION,
  JOB_DURATION,
  jobExecutionStates,
} from "../lib/construct-groups/job-execution-states";
import { jsonataStateMachine } from "../lib/construct-groups/workflow-states";
import { evaluate } from "./jsonata-evaluator";
import {
  AslTestState,
  reachableFrom,
  renderedDefinition,
  structuralProblems,
  taskStates,
} from "./state-machine-definition";

const TENANT_ACCOUNT = "111122223333";
const JOB_ID = "ccj-0fdef5d64630";
const EXECUTION_INPUT = {
  tenant_arn_prefix: `arn:aws:ccf:us-east-1:${TENANT_ACCOUNT}`,
  destination_id: "dst-7c41e9a2b5d0",
  connector_id: "cc-3a9e1f0b2c7d",
  job_id: JOB_ID,
  correlation_id: "correlation-1",
};
const SOURCE_ROLE = `arn:aws:iam::${TENANT_ACCOUNT}:role/CcfSourceRole-github`;
const SECRET = `arn:aws:secretsmanager:us-east-1:${TENANT_ACCOUNT}:secret:github-token`;
const LOG_GROUP = `arn:aws:logs:us-east-1:${TENANT_ACCOUNT}:log-group:/ccf/connectors/github`;
const WHERE = `see log group /ccf/connectors/github, stream ${JOB_ID}`;
/** The custom connector item `ReadConnector` returns, in DynamoDB's attribute value form. */
const CONNECTOR_ITEM = {
  source_access: { M: { role_arn: { S: SOURCE_ROLE }, secret_arn: { S: SECRET } } },
  log_destination: { M: { arn: { S: LOG_GROUP } } },
};

/** Stands in for `IngestionQueue.requestIngestion`, with the two state names it builds. */
const fakeIngestion = {
  requestIngestion: (scope: Construct, props: RequestIngestionProps) =>
    Pass.jsonata(scope, `${props.statePrefix}SetIngestionPending`).next(
      Pass.jsonata(scope, `${props.statePrefix}SendIngestionMessage`).next(props.next),
    ),
};

const stack = new Stack(new App(), "Jobs", {
  env: { account: "444455556666", region: "us-east-1" },
});
const table = (id: string, sortKey: string): Table =>
  new Table(stack, id, {
    partitionKey: { name: "tenant_arn_prefix", type: AttributeType.STRING },
    sortKey: { name: sortKey, type: AttributeType.STRING },
  });
jsonataStateMachine(stack, "Machine", {
  stateMachineName: JobExecutionName.STATE_MACHINE,
  definition: jobExecutionStates(stack, {
    jobsTable: table("Jobs", "job_id"),
    connectorsTable: table("Connectors", "destination_connector_key"),
    jobQueueArn: "arn:aws:batch:us-east-1:444455556666:job-queue/CcfJobQueue",
    recordRetention: Duration.days(30),
    activeRecordRetention: Duration.days(37),
    ingestion: fakeIngestion,
  }),
  role: new Role(stack, "Role", { assumedBy: new ServicePrincipal("states.amazonaws.com") }),
  logRetention: RetentionDays.ONE_WEEK,
  removalPolicy: RemovalPolicy.DESTROY,
});
const definition = renderedDefinition(Template.fromStack(stack), JobExecutionName.STATE_MACHINE);
const state = (name: string): AslTestState => definition.States[name];
const context = { Execution: { Input: EXECUTION_INPUT } };

/** A Batch job detail as `submitJob.sync` returns it, for a job with ecsProperties. */
const batchDetail = (overrides: Record<string, unknown> = {}): Record<string, unknown> => ({
  JobId: "batch-job-1",
  Status: "SUCCEEDED",
  StartedAt: 1_762_000_000_000,
  StoppedAt: 1_762_000_090_500,
  EcsProperties: {
    TaskProperties: [
      {
        ExecutionRoleArn: "arn:aws:iam::444455556666:role/ccf/CcfBatchExecutionRole",
        TaskRoleArn: "arn:aws:iam::444455556666:role/ccf-runtime/CcfConnectorRuntime-1",
        Containers: [
          { Name: "log-router", ExitCode: 0 },
          { Name: "connector", ExitCode: 0 },
        ],
      },
    ],
  },
  ...overrides,
});

interface ReasonRequest {
  readonly exitCode?: number;
  readonly batchReason?: string;
  /** The item ReadConnector read; left out when it couldn't read one. */
  readonly connector?: unknown;
}

const reason = ({ exitCode, batchReason, connector }: ReasonRequest): Promise<unknown> =>
  evaluate({
    expression: FAILURE_REASON_EXPRESSION,
    bindings: {
      states: {
        context,
        input: {
          outcome: "FAILED",
          run: { exit_code: exitCode, batch_reason: batchReason },
          ...(connector === undefined ? {} : { connector }),
        },
      },
    },
  });

describe("CcfJobExecution's failure reason", () => {
  test.each([
    [70, undefined, `ConnectorImplementationError: ${WHERE}`],
    [71, undefined, `JobTerminated: ${WHERE}`],
    [
      71,
      "Job attempt duration exceeded timeout",
      `JobTerminated: Job attempt duration exceeded timeout; ${WHERE}`,
    ],
    [72, undefined, `SourceAccessDenied: AccessDenied assuming ${SOURCE_ROLE}`],
    [73, undefined, `SourceAccessFailed: AssumeRole failed for ${SOURCE_ROLE}`],
    [74, undefined, `SecretUnavailable: GetSecretValue failed for ${SECRET}`],
    [
      76,
      undefined,
      "FrameworkApiUnavailable: the job couldn't read its inputs from, or send its records to, the framework API",
    ],
  ])("exit code %p with Batch reason %p is %p", async (exitCode, batchReason, expected) => {
    expect(await reason({ exitCode, batchReason, connector: CONNECTOR_ITEM })).toBe(expected);
  });

  test("names no log group when the custom connector has no log destination", async () => {
    const connector = { source_access: CONNECTOR_ITEM.source_access };

    expect(await reason({ exitCode: 70, connector })).toBe(
      "ConnectorImplementationError: no log destination is set",
    );
  });

  test.each([
    ["no exit code", undefined],
    ["an unknown exit code", 1],
  ])("is Batch's reason for %s", async (_, exitCode) => {
    expect(
      await reason({
        exitCode,
        batchReason: "Essential container in task exited",
        connector: CONNECTOR_ITEM,
      }),
    ).toBe("Essential container in task exited");
  });

  test("is Batch's reason when ReadConnector couldn't read the custom connector", async () => {
    expect(await reason({ exitCode: 72, batchReason: "Task failed" })).toBe("Task failed");
  });
});

describe("CcfJobExecution's RunJob", () => {
  test("sends every failure to ReadConnector with Step Functions' error output", () => {
    expect(state("RunJob").Catch).toEqual([{ ErrorEquals: ["States.ALL"], Next: "ReadConnector" }]);
  });

  test("records the connector container's exit code, the roles, and the times from a succeeded job", async () => {
    const output = await evaluate({
      expression: String(state("RunJob").Output),
      bindings: { states: { context, result: batchDetail() } },
    });

    expect(output).toEqual({
      outcome: "SUCCEEDED",
      run: {
        batch_job_id: "batch-job-1",
        execution_role_arn: "arn:aws:iam::444455556666:role/ccf/CcfBatchExecutionRole",
        job_role_arn: "arn:aws:iam::444455556666:role/ccf-runtime/CcfConnectorRuntime-1",
        started_at: 1_762_000_000_000,
        stopped_at: 1_762_000_090_500,
        exit_code: 0,
      },
    });
  });

  test("ReadConnector records a failed Batch job's detail and the custom connector", async () => {
    const detail = batchDetail({
      Status: "FAILED",
      StatusReason: "Essential container in task exited",
    });

    expect(
      await evaluate({
        expression: String(state("ReadConnector").Output),
        bindings: {
          states: {
            context,
            input: { Error: "States.TaskFailed", Cause: JSON.stringify(detail) },
            result: { Item: CONNECTOR_ITEM },
          },
        },
      }),
    ).toMatchObject({
      outcome: "FAILED",
      run: {
        batch_job_id: "batch-job-1",
        exit_code: 0,
        batch_reason: "Essential container in task exited",
      },
      connector: CONNECTOR_ITEM,
    });
  });

  test("ReadConnector records any other error by its name, and so does its own catch", async () => {
    const [caught] = state("ReadConnector").Catch ?? [];
    const failed = await evaluate({
      expression: String(caught.Output),
      bindings: { states: { context, input: { Error: "Batch.ClientException", Cause: "no" } } },
    });

    expect(failed).toEqual({ outcome: "FAILED", run: { batch_reason: "Batch.ClientException" } });
  });

  test("overrides the job ID variable on both containers and tags the job", () => {
    expect(state("RunJob").Arguments).toMatchObject({
      EcsPropertiesOverride: {
        TaskProperties: [
          {
            Containers: [
              { Name: "connector", Environment: [{ Name: "CUSTOM_CONNECTOR_FRAMEWORK_JOB_ID" }] },
              { Name: "log-router", Environment: [{ Name: "CUSTOM_CONNECTOR_FRAMEWORK_JOB_ID" }] },
            ],
          },
        ],
      },
      Tags: { "ccf:owned-by": "custom-connector-framework" },
    });
  });

  test("retries only Batch's throttling, and every other task retries the shared transient errors", () => {
    expect(state("RunJob").Retry?.map((retry) => retry.ErrorEquals)).toEqual([
      ["Batch.TooManyRequestsException"],
    ]);
    for (const [name, task] of taskStates(definition).filter(
      ([candidate]) => candidate !== "RunJob",
    ))
      expect({
        name,
        timeout: task.Retry?.some((retry) => retry.ErrorEquals.includes("States.Timeout")),
      }).toEqual({
        name,
        timeout: true,
      });
  });
});

describe("CcfJobExecution's end", () => {
  test("a STOPPING job whose Batch job succeeded ends STOPPED with no ingestion request", async () => {
    const [canceled] = state("FinishRunning").Catch ?? [];
    const stopped = await evaluate({
      expression: String(state("FinishStopped").Output),
      bindings: { states: { context, input: { outcome: "SUCCEEDED", run: { exit_code: 0 } } } },
    });
    const [succeeded] = state("RequestIngestionWhenSucceeded").Choices ?? [];

    expect([canceled.ErrorEquals, canceled.Next]).toEqual([
      ["DynamoDb.TransactionCanceledException"],
      "FinishStopped",
    ]);
    expect(stopped).toMatchObject({ outcome: "STOPPED" });
    expect(
      await evaluate({
        expression: String(succeeded.Condition),
        bindings: { states: { input: stopped } },
      }),
    ).toBe(false);
  });

  test("a succeeded job reaches the ingestion request's states", async () => {
    const [succeeded] = state("RequestIngestionWhenSucceeded").Choices ?? [];

    expect(
      await evaluate({
        expression: String(succeeded.Condition),
        bindings: { states: { input: { outcome: "SUCCEEDED" } } },
      }),
    ).toBe(true);
    expect(succeeded.Next).toBe("JobSetIngestionPending");
    expect([...reachableFrom(definition, "RunJob")]).toEqual(
      expect.arrayContaining([
        "JobSetIngestionPending",
        "JobSendIngestionMessage",
        "PublishMetrics",
      ]),
    );
  });

  test("a succeeded job's finishing values use only DynamoDB member names Step Functions accepts", async () => {
    const values = (
      state("FinishRunning").Arguments as {
        TransactItems: { Update: { ExpressionAttributeValues: Record<string, unknown> } }[];
      }
    ).TransactItems[0].Update.ExpressionAttributeValues;
    const input = { outcome: "SUCCEEDED", run: { exit_code: 0 } };
    const rendered = await Promise.all(
      [values[":reason"], values[":execution"]].map((value) =>
        evaluate({ expression: String(value), bindings: { states: { context, input } } }),
      ),
    );
    const sdkMembers = new Set(["S", "N", "B", "Ss", "Ns", "Bs", "M", "L", "Null", "Bool"]);
    const memberNames = (value: unknown): string[] =>
      value !== null && typeof value === "object"
        ? Object.entries(value).flatMap(([name, inner]) => [name, ...memberNames(inner)])
        : [];

    expect(rendered).toEqual([{ Null: true }, { Null: true }]);
    expect(rendered.flatMap(memberNames).filter((name) => !sdkMembers.has(name))).toEqual([]);
  });

  test("both terminal transactions release the custom connector only while this job holds it", () => {
    for (const name of ["FinishRunning", "FinishStopped"]) {
      const [, release] = (
        state(name).Arguments as { TransactItems: { Update: Record<string, unknown> }[] }
      ).TransactItems;

      expect(release.Update.ConditionExpression).toBe(
        "#status = :in_use AND active_job_id = :job_id",
      );
    }
  });

  test("JobDuration is StoppedAt - StartedAt in seconds, and 0 when Batch never started the job", async () => {
    const duration = (run: Record<string, unknown>): Promise<unknown> =>
      evaluate({ expression: JOB_DURATION, bindings: { states: { input: { run } } } });

    expect(await duration({ started_at: 1_762_000_000_000, stopped_at: 1_762_000_090_500 })).toBe(
      90.5,
    );
    expect(await duration({})).toBe(0);
  });

  test("reads no execution input field outside JobExecutionInput", () => {
    const fields = [
      ...JSON.stringify(definition).matchAll(/\$states\.context\.Execution\.Input\.([a-z_]+)/g),
    ].map((match) => match[1]);

    expect(new Set(fields)).toEqual(new Set(Object.keys(EXECUTION_INPUT)));
  });

  test("has no structural problem", () => {
    expect(structuralProblems(definition)).toEqual([]);
  });
});
