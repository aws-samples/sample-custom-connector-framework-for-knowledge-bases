import { Duration } from "aws-cdk-lib";
import {
  Dashboard,
  GraphWidget,
  IMetric,
  MathExpression,
  Metric,
  TextWidget,
} from "aws-cdk-lib/aws-cloudwatch";
import { Construct } from "constructs";
import { ProjectName, ResourceName, createResourceName } from "../common/config";
import {
  enumMember,
  frameworkDimensionName,
  frameworkMetricName,
  metricsContract,
} from "../common/contracts";
import { DELIVERY_RUNBOOK } from "./service-alarms";

/** One-minute resolution for an incident; the alarms evaluate five-minute periods. */
const DASHBOARD_PERIOD = Duration.minutes(1);
const WIDGET_WIDTH = 12;
const FULL_WIDTH = 24;

/** The terminal job statuses `JobsCompleted` carries as its `Status` dimension. */
const terminalJobStatuses = (): readonly string[] =>
  ["SUCCEEDED", "FAILED", "STOPPED"].map((member) => enumMember({ schema: "JobStatus", member }));

/** A framework metric with exactly the given dimensions, summed. */
const frameworkMetric = (request: {
  readonly key: string;
  readonly dimensions?: Record<string, string>;
  readonly label?: string;
}): Metric =>
  new Metric({
    namespace: metricsContract().namespace,
    metricName: frameworkMetricName(request.key),
    dimensionsMap: request.dimensions,
    statistic: "Sum",
    period: DASHBOARD_PERIOD,
    label: request.label,
  });

/**
 * A framework metric over every value of its dimensions, through a CloudWatch `SEARCH`
 * expression, because those dimensions (such as `ConnectorId`) aren't known at synth.
 */
const searchedMetric = (request: {
  readonly key: string;
  readonly dimensionKeys: readonly string[];
  readonly statistic: string;
  readonly label: string;
}): IMetric => {
  const dimensions = request.dimensionKeys.map(frameworkDimensionName).join(",");
  return new MathExpression({
    expression: `SEARCH('{${metricsContract().namespace},${dimensions}} MetricName="${frameworkMetricName(request.key)}"', '${request.statistic}', ${DASHBOARD_PERIOD.toSeconds()})`,
    usingMetrics: {},
    label: request.label,
    period: DASHBOARD_PERIOD,
  });
};

/**
 * The framework's operations dashboard. It charts only the framework's server-side metrics: no
 * toolkit or connector metric, because those live in the tenant accounts.
 */
export class OperationsDashboard extends Construct {
  constructor(scope: Construct, id: string) {
    super(scope, id);
    const dashboard = new Dashboard(this, "Dashboard", {
      dashboardName: createResourceName({
        projectName: ProjectName.CCF,
        resourceName: ResourceName.OPERATIONS_DASHBOARD,
      }),
      defaultInterval: Duration.hours(3),
    });
    dashboard.addWidgets(
      new TextWidget({
        markdown: `# Custom Connector Framework\n\nEvery alarm links to its section of \`${DELIVERY_RUNBOOK}\`.`,
        width: FULL_WIDTH,
        height: 2,
      }),
    );
    dashboard.addWidgets(
      new GraphWidget({
        title: "Jobs completed, by status",
        width: WIDGET_WIDTH,
        left: terminalJobStatuses().map((status) =>
          frameworkMetric({
            key: "JOBS_COMPLETED",
            dimensions: { [frameworkDimensionName("STATUS")]: status },
            label: status,
          }),
        ),
      }),
      new GraphWidget({
        title: "Job duration (seconds)",
        width: WIDGET_WIDTH,
        left: ["p50", "p99"].map((statistic) =>
          searchedMetric({
            key: "JOB_DURATION",
            dimensionKeys: ["CONNECTOR_ID"],
            statistic,
            label: statistic,
          }),
        ),
      }),
    );
    dashboard.addWidgets(
      new GraphWidget({
        title: "Documents and collections recorded by the API",
        width: WIDGET_WIDTH,
        left: ["DOCUMENTS_RECORDED", "DOCUMENTS_DELETED", "COLLECTIONS_DELETED"].map((key) =>
          searchedMetric({
            key,
            dimensionKeys: ["CONNECTOR_ID"],
            statistic: "Sum",
            label: frameworkMetricName(key),
          }),
        ),
      }),
      new GraphWidget({
        title: "API errors, by operation and error code",
        width: WIDGET_WIDTH,
        left: [
          searchedMetric({
            key: "API_ERRORS",
            dimensionKeys: ["OPERATION", "ERROR_CODE"],
            statistic: "Sum",
            label: "",
          }),
        ],
      }),
    );
    dashboard.addWidgets(
      new GraphWidget({
        title: "Destination creation failures",
        width: WIDGET_WIDTH,
        left: [frameworkMetric({ key: "DESTINATION_CREATION_FAILED" })],
      }),
      new GraphWidget({
        title: "Bedrock ingestion jobs",
        width: WIDGET_WIDTH,
        left: ["INGESTION_JOBS_STARTED", "INGESTION_JOBS_FAILED"].map((key) =>
          frameworkMetric({ key, label: frameworkMetricName(key) }),
        ),
      }),
    );
  }
}
