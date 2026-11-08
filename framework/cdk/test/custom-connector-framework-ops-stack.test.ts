import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { Match, Template } from "aws-cdk-lib/assertions";
import { loadFrameworkAppConfig } from "../lib/common/app-config";
import { metricsContract } from "../lib/common/contracts";
import { FrameworkExport } from "../lib/common/framework-exports";
import { DELIVERY_RUNBOOK } from "../lib/construct-groups/service-alarms";
import { CustomConnectorFrameworkOpsStack } from "../lib/stacks/custom-connector-framework-ops-stack";
import { SERVICE_ACCOUNT, createApp, importedNames } from "./construct-fixtures";

type JsonObject = Record<string, unknown>;

const config = loadFrameworkAppConfig({
  path: resolve(__dirname, "..", "config", "default.json"),
  serviceAccount: SERVICE_ACCOUNT,
});
const ops = Template.fromStack(
  new CustomConnectorFrameworkOpsStack(createApp(), "CustomConnectorFrameworkOps", {
    alarms: config.alarms,
    env: { account: SERVICE_ACCOUNT, region: "us-east-1" },
  }),
);
const alarms = Object.values(ops.findResources("AWS::CloudWatch::Alarm")).map(
  (alarm) => alarm.Properties as JsonObject,
);
const imported = (name: FrameworkExport): JsonObject => ({ "Fn::ImportValue": name });
const { namespace } = metricsContract();
const FRAMEWORK_METRICS = new Set(
  Object.values(metricsContract().metrics).map((metric) => metric.name),
);

/** The dashboard body, with each CloudFormation reference replaced by a placeholder. */
const dashboardBody = (): { widgets: { properties: { metrics?: unknown[][] } }[] } => {
  const flatten = (value: unknown): string => {
    if (typeof value === "string") return value;
    const join = (value as { "Fn::Join"?: [string, unknown[]] })["Fn::Join"];
    if (join !== undefined) return join[1].map(flatten).join(join[0]);
    return "PLACEHOLDER";
  };
  const [dashboard] = Object.values(ops.findResources("AWS::CloudWatch::Dashboard"));
  return JSON.parse(flatten(dashboard.Properties.DashboardBody));
};

describe("CustomConnectorFrameworkOps", () => {
  it("alarms when more than the configured percent of API requests fail with a 5xx", () => {
    const [alarm] = alarms.filter((properties) =>
      JSON.stringify(properties.Metrics ?? []).includes('"MetricName":"5XXError"'),
    );

    expect(alarm).toMatchObject({
      Threshold: config.alarms.apiServerErrorPercent,
      ComparisonOperator: "GreaterThanThreshold",
      Metrics: expect.arrayContaining([
        expect.objectContaining({ Expression: "IF(requests > 0, 100 * errors / requests, 0)" }),
      ]),
    });
  });

  test("JobsFailed alarms at P2 on JobsCompleted with Status FAILED", () => {
    ops.hasResourceProperties("AWS::CloudWatch::Alarm", {
      Namespace: namespace,
      MetricName: "JobsCompleted",
      Dimensions: [{ Name: "Status", Value: "FAILED" }],
      AlarmDescription: Match.stringLikeRegexp("^\\[P2\\] .+#failed-jobs$"),
      EvaluationPeriods: 1,
      DatapointsToAlarm: 1,
    });
  });

  test("DestinationCreationFailed alarms at P2 on the metric with no dimension", () => {
    const [alarm] = alarms.filter(
      (properties) => properties.MetricName === "DestinationCreationFailed",
    );
    expect(alarm.Namespace).toBe(namespace);
    expect(alarm.Dimensions).toBeUndefined();
    expect(alarm.AlarmDescription).toMatch(/^\[P2\] .+#destination-creation-failures$/);
  });

  test("a message in the ingestion dead-letter queue alarms at P1", () => {
    ops.hasResourceProperties("AWS::CloudWatch::Alarm", {
      Namespace: "AWS/SQS",
      MetricName: "ApproximateNumberOfMessagesVisible",
      Dimensions: [{ Name: "QueueName", Value: imported(FrameworkExport.INGESTION_DLQ_NAME) }],
      AlarmDescription: Match.stringLikeRegexp("^\\[P1\\] "),
    });
    ops.resourcePropertiesCountIs("AWS::CloudWatch::Alarm", { Namespace: "AWS/SQS" }, 2);
  });

  test("each workflow's failed executions alarm at P2, the job execution's included", () => {
    for (const name of [
      FrameworkExport.DESTINATION_WORKFLOW_ARN,
      FrameworkExport.CONNECTOR_DELETION_WORKFLOW_ARN,
      FrameworkExport.JOB_EXECUTION_ARN,
    ])
      ops.hasResourceProperties("AWS::CloudWatch::Alarm", {
        Namespace: "AWS/States",
        MetricName: "ExecutionsFailed",
        Dimensions: [{ Name: "StateMachineArn", Value: imported(name) }],
        AlarmDescription: Match.stringLikeRegexp("^\\[P2\\] .+#workflow-execution-failures$"),
      });
  });

  test("keeps the delivery, Lambda, and API alarms, and none on a job Lambda or job queue", () => {
    ops.resourcePropertiesCountIs("AWS::CloudWatch::Alarm", { Namespace: "AWS/Scheduler" }, 1);
    ops.resourcePropertiesCountIs("AWS::CloudWatch::Alarm", { Namespace: "AWS/Lambda" }, 3);
    expect(JSON.stringify(alarms)).not.toMatch(/JobOrchestrator|JobStatus|JobsStream|BatchStatus/);
    ops.hasResourceProperties("AWS::CloudWatch::Alarm", {
      Namespace: "AWS/ApiGateway",
      MetricName: "Latency",
      ExtendedStatistic: "p99",
      Threshold: config.alarms.apiLatencyMs,
    });
  });

  test("every alarm links a runbook section that exists, and notifies the topic on ALARM and OK", () => {
    const runbook = readFileSync(
      resolve(
        __dirname,
        "..",
        "..",
        "..",
        "docs",
        "administrator-guide",
        "runbook-delivery-failures.md",
      ),
      "utf8",
    );
    const sections = new Set([...runbook.matchAll(/^## ([a-z-]+)$/gm)].map((match) => match[1]));
    for (const properties of alarms) {
      const description = properties.AlarmDescription as string;
      expect(description.startsWith("[P1] ") || description.startsWith("[P2] ")).toBe(true);
      expect(description).toContain(` Runbook: ${DELIVERY_RUNBOOK}#`);
      expect(sections).toContain(description.split("#").pop());
      expect(properties.AlarmActions).toHaveLength(1);
      expect(properties.OKActions).toHaveLength(1);
    }
  });

  test("exports the alarm topic ARN", () => {
    const outputs = Object.values(ops.toJSON().Outputs as Record<string, JsonObject>);
    expect(outputs.map((output) => (output.Export as JsonObject | undefined)?.Name)).toContain(
      FrameworkExport.ALARM_TOPIC_ARN,
    );
  });

  test("every dashboard metric is a framework metric in the framework namespace", () => {
    const names: string[] = [];
    for (const widget of dashboardBody().widgets)
      for (const metric of widget.properties.metrics ?? []) {
        const [first] = metric;
        if (typeof first === "string") {
          expect(first).toBe(namespace);
          names.push(metric[1] as string);
        } else {
          const expression = (first as { expression: string }).expression;
          expect(expression.startsWith(`SEARCH('{${namespace},`)).toBe(true);
          const name = /MetricName="(\w+)"/.exec(expression)?.[1];
          expect(name).toBeDefined();
          names.push(name ?? "");
        }
      }
    expect(names.length).toBeGreaterThan(0);
    for (const name of names) expect(FRAMEWORK_METRICS).toContain(name);
    expect(JSON.stringify(dashboardBody())).not.toMatch(/CustomConnectorToolkit|GitHubConnector/);
  });

  test("reads only the service stack's export names", () => {
    const names = importedNames(ops);
    expect(names.length).toBeGreaterThan(0);
    for (const name of names) expect(Object.values(FrameworkExport)).toContain(name);
    expect(names).not.toContain(FrameworkExport.ALARM_TOPIC_ARN);
  });

  test("publishes alarms only over TLS", () => {
    ops.hasResourceProperties("AWS::SNS::TopicPolicy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Effect: "Deny",
            Condition: { Bool: { "aws:SecureTransport": "false" } },
          }),
        ]),
      },
    });
  });
});
