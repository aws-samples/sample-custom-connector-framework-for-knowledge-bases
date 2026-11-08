import { Aws, Duration } from "aws-cdk-lib";
import { ITable } from "aws-cdk-lib/aws-dynamodb";
import {
  Choice,
  Condition,
  CustomState,
  Errors,
  Fail,
  IChainable,
  Succeed,
} from "aws-cdk-lib/aws-stepfunctions";
import { Construct } from "constructs";
import { CallAwsService } from "aws-cdk-lib/aws-stepfunctions-tasks";
import { BATCH_OWNERSHIP_TAG, JobExecutionName } from "../common/config";
import {
  enumMember,
  frameworkDimensionName,
  frameworkMetricName,
  jobContainerContract,
  metricsContract,
  tablesContract,
} from "../common/contracts";
import { RequestIngestionProps } from "./ingestion-queue";
import { RETRY_POLICY, SdkService, sdkCall, sdkError } from "./workflow-states";

/** The ingestion request builder `IngestionQueue` offers; the job requests one when it succeeds. */
export interface JobIngestionRequests {
  requestIngestion(scope: Construct, props: RequestIngestionProps): IChainable;
}

export interface JobExecutionStatesProps {
  /** `CcfCustomConnectorJobs`, whose job the execution moves from `STARTED` to its end. */
  readonly jobsTable: ITable;
  /** `CcfCustomConnectors`, whose custom connector the job releases when it ends. */
  readonly connectorsTable: ITable;
  readonly jobQueueArn: string;
  /** How long a job's record outlives the job's end. */
  readonly recordRetention: Duration;
  /** How long a running job's record lives: the record retention plus the longest job timeout. */
  readonly activeRecordRetention: Duration;
  readonly ingestion: JobIngestionRequests;
}

/** The execution input, `JobExecutionInput`; a state's own input changes after a catch. */
const INPUT = "$states.context.Execution.Input";
const input = (member: string): string => `${INPUT}.${member}`;
const jobStatus = (member: string): string => enumMember({ schema: "JobStatus", member });
const connectorStatus = (member: string): string =>
  enumMember({ schema: "ConnectorStatus", member });
const SUCCEEDED = jobStatus("SUCCEEDED");
const NOW = "{% $string($millis() / 1000) %}";
const ttl = (retention: Duration): string =>
  `{% $string($floor($millis() / 1000) + ${retention.toSeconds()}) %}`;
const CONDITION_FAILED = sdkError({
  service: SdkService.DYNAMODB,
  errorName: "ConditionalCheckFailedException",
});
const TRANSACTION_CANCELED = sdkError({
  service: SdkService.DYNAMODB,
  errorName: "TransactionCanceledException",
});
/** AWS Batch's throttling, the one `RunJob` error retried: `SubmitJob` takes no client token. */
const BATCH_THROTTLED = "Batch.TooManyRequestsException";

const contract = jobContainerContract();
const exitCode = (name: string): number => contract.exitCodes[name];

/**
 * What a job's end records from a Batch job detail `$detail`: its IDs, roles, times in
 * milliseconds, the connector container's exit code, and Batch's reason.
 */
const runOf = (detail: string): string =>
  `($d := ${detail}; $task := $d.EcsProperties.TaskProperties[0]; ` +
  "{'batch_job_id': $d.JobId, 'execution_role_arn': $task.ExecutionRoleArn, " +
  "'job_role_arn': $task.TaskRoleArn, 'started_at': $d.StartedAt, 'stopped_at': $d.StoppedAt, " +
  `'exit_code': $task.Containers[Name = '${contract.containers.connector}'].ExitCode, ` +
  "'batch_reason': $d.StatusReason})";

/**
 * A failed `RunJob`'s record, from Step Functions' error output `{Error, Cause}`: a Batch job that
 * failed carries its job detail as the cause; any other error records only its name.
 */
const FAILED_RUN =
  `($states.input.Error = '${Errors.TASKS_FAILED}' ? ${runOf("$parse($states.input.Cause)")} : ` +
  "{'batch_reason': $states.input.Error})";

/** A failed job's `status_reason` as a JSONata block, without its `{% %}`. */
const FAILURE_REASON =
  "($run := $states.input.run; $item := $states.input.connector; $batch := $run.batch_reason; " +
  "$logArn := $item.log_destination.M.arn.S; $role := $item.source_access.M.role_arn.S; " +
  "$secret := $item.source_access.M.secret_arn.S; $code := $run.exit_code; " +
  "$where := $logArn ? 'see log group ' & $substringAfter($logArn, ':log-group:') & ', stream ' & " +
  `${input("job_id")} : 'no log destination is set'; ` +
  "$not($exists($item)) ? $batch : " +
  `$code = ${exitCode("CONNECTOR_IMPLEMENTATION_ERROR")} ? 'ConnectorImplementationError: ' & $where : ` +
  `$code = ${exitCode("JOB_TERMINATED")} ? 'JobTerminated: ' & ($batch ? $batch & '; ' : '') & $where : ` +
  `$code = ${exitCode("SOURCE_ACCESS_DENIED")} ? 'SourceAccessDenied: AccessDenied assuming ' & $role : ` +
  `$code = ${exitCode("SOURCE_ACCESS_FAILED")} ? 'SourceAccessFailed: AssumeRole failed for ' & $role : ` +
  `$code = ${exitCode("SECRET_UNAVAILABLE")} and $secret ? 'SecretUnavailable: GetSecretValue failed for ' & $secret : ` +
  `$code = ${exitCode("FRAMEWORK_API_UNAVAILABLE")} ? "FrameworkApiUnavailable: the job couldn't read its inputs ` +
  `from, or send its records to, the framework API" : $batch)`;

/**
 * A failed job's `status_reason`, from its connector container's exit code, the custom
 * connector `ReadConnector` read, and Batch's reason; Batch's reason alone for any other code,
 * or when the custom connector couldn't be read.
 */
export const FAILURE_REASON_EXPRESSION = `{% ${FAILURE_REASON} %}`;

/**
 * DynamoDB's NULL value as Step Functions' SDK integration names it: members are the SDK's
 * PascalCase names (`Null`, `Bool`, `Ss`), so the wire name `NULL` fails the task before it starts.
 */
const NULL_VALUE = "{'Null': true}";

/** `status_reason` as a DynamoDB value: the failure reason, or NULL for a succeeded job or no reason. */
const REASON_VALUE =
  `{% ($reason := ${FAILURE_REASON}; ` +
  `$states.input.outcome = '${SUCCEEDED}' or $not($exists($reason)) ? ${NULL_VALUE} : {'S': $reason}) %}`;

/** `service_execution` as a DynamoDB value, or NULL when Batch never reported the job's roles. */
const SERVICE_EXECUTION_VALUE =
  "{% ($run := $states.input.run; " +
  `$seconds := function($ms) { $exists($ms) ? {'N': $string($ms / 1000)} : ${NULL_VALUE} }; ` +
  "$exists($run.batch_job_id) and $exists($run.execution_role_arn) and $exists($run.job_role_arn) ? " +
  "{'M': {'execution_id': {'S': $run.batch_job_id}, 'execution_role_arn': {'S': $run.execution_role_arn}, " +
  "'job_role_arn': {'S': $run.job_role_arn}, 'started_at': $seconds($run.started_at), " +
  `'completed_at': $seconds($run.stopped_at)}} : ${NULL_VALUE}) %}`;

/** `JobDuration`: `StoppedAt - StartedAt` in seconds, or 0 when Batch never started the job. */
export const JOB_DURATION =
  "{% $exists($states.input.run.started_at) and $exists($states.input.run.stopped_at) ? " +
  "($states.input.run.stopped_at - $states.input.run.started_at) / 1000 : 0 %}";

const keys = (table: "jobs" | "connectors"): Record<string, unknown> => {
  const { partitionKey, sortKey } = tablesContract()[table];
  const sort =
    table === "jobs"
      ? input("job_id")
      : `${input("destination_id")} & '#' & ${input("connector_id")}`;
  return {
    [partitionKey.name]: { S: `{% ${input("tenant_arn_prefix")} %}` },
    ...(sortKey === null ? {} : { [sortKey.name]: { S: `{% ${sort} %}` } }),
  };
};

/** One of the two terminal transactions: the job's update, and what the state outputs after it. */
interface FinishRequest {
  readonly id: string;
  readonly update: Readonly<Record<string, unknown>>;
  readonly outputs: string;
}

/**
 * `CcfJobExecution`'s definition: mark the job `RUNNING`, run its Batch job and wait, record the
 * outcome and release the custom connector in one transaction, request an ingestion when the job
 * succeeded, and publish the job's metrics. A stop that arrives first ends the job `STOPPED`.
 */
export const jobExecutionStates = (
  scope: Construct,
  props: JobExecutionStatesProps,
): IChainable => {
  const { jobsTable, connectorsTable, jobQueueArn, recordRetention, activeRecordRetention } = props;
  const names = { "#status": "status", "#ttl": "ttl" };
  const dynamodb = {
    service: SdkService.DYNAMODB,
    iamResources: [jobsTable.tableArn, connectorsTable.tableArn],
  };
  const release = {
    Update: {
      TableName: connectorsTable.tableName,
      Key: keys("connectors"),
      UpdateExpression:
        "SET #status = :available, updated_at = :now REMOVE active_job_id ADD #version :one",
      ConditionExpression: "#status = :in_use AND active_job_id = :job_id",
      ExpressionAttributeNames: { "#status": "status", "#version": "version" },
      ExpressionAttributeValues: {
        ":available": { S: connectorStatus("AVAILABLE") },
        ":in_use": { S: connectorStatus("IN_USE") },
        ":job_id": { S: `{% ${input("job_id")} %}` },
        ":now": { N: NOW },
        ":one": { N: "1" },
      },
    },
  };
  const finish = ({ id, update, outputs }: FinishRequest): CallAwsService =>
    sdkCall(scope, id, {
      ...dynamodb,
      action: "transactWriteItems",
      arguments: {
        TransactItems: [
          { Update: { TableName: jobsTable.tableName, Key: keys("jobs"), ...update } },
          release,
        ],
      },
      outputs,
    });
  const done = Succeed.jsonata(scope, "Done");
  const notFinished = Fail.jsonata(scope, "JobNotFinished", {
    error: "JobNotFinished",
    cause: `{% 'The job ' & ${input("job_id")} & ' was not recorded as ended (correlation ' & ${input("correlation_id")} & '). StopCustomConnectorJob ends it STOPPED and releases its custom connector.' %}`,
  });
  const dimension = ({ key, value }: { readonly key: string; readonly value: string }) => ({
    Name: frameworkDimensionName(key),
    Value: value,
  });
  const status = dimension({ key: "STATUS", value: "{% $states.input.outcome %}" });
  const connector = dimension({ key: "CONNECTOR_ID", value: `{% ${input("connector_id")} %}` });
  const publishMetrics = sdkCall(scope, "PublishMetrics", {
    service: SdkService.CLOUDWATCH,
    action: "putMetricData",
    iamResources: ["*"],
    arguments: {
      Namespace: metricsContract().namespace,
      MetricData: [
        {
          MetricName: frameworkMetricName("JOBS_COMPLETED"),
          Dimensions: [status],
          Value: 1,
          Unit: "Count",
        },
        {
          MetricName: frameworkMetricName("JOBS_COMPLETED"),
          Dimensions: [status, connector],
          Value: 1,
          Unit: "Count",
        },
        {
          MetricName: frameworkMetricName("JOB_DURATION"),
          Dimensions: [connector],
          Value: JOB_DURATION,
          Unit: "Seconds",
        },
      ],
    },
  });
  publishMetrics
    .addCatch(done, { errors: [Errors.ALL], outputs: "{% $states.input %}" })
    .next(done);
  const requestIngestion = Choice.jsonata(scope, "RequestIngestionWhenSucceeded")
    .when(
      Condition.jsonata(`{% $states.input.outcome = '${SUCCEEDED}' %}`),
      props.ingestion.requestIngestion(scope, {
        statePrefix: "Job",
        tenantArnPrefix: input("tenant_arn_prefix"),
        destinationId: input("destination_id"),
        next: publishMetrics,
      }),
    )
    .otherwise(publishMetrics);

  const finishStopped = finish({
    id: "FinishStopped",
    update: {
      UpdateExpression: "SET #status = :stopped, updated_at = :now, #ttl = :ttl",
      ConditionExpression: "#status = :stopping",
      ExpressionAttributeNames: names,
      ExpressionAttributeValues: {
        ":stopped": { S: jobStatus("STOPPED") },
        ":stopping": { S: jobStatus("STOPPING") },
        ":now": { N: NOW },
        ":ttl": { N: ttl(recordRetention) },
      },
    },
    // A stop that arrived after a succeeded run requests no ingestion.
    outputs: `{% {'outcome': '${jobStatus("STOPPED")}', 'run': $states.input.run} %}`,
  });
  finishStopped.addCatch(done, { errors: [TRANSACTION_CANCELED], outputs: "{% $states.input %}" });
  finishStopped.addCatch(notFinished, { errors: [Errors.ALL] });
  finishStopped.next(requestIngestion);
  const finishRunning = finish({
    id: "FinishRunning",
    update: {
      UpdateExpression:
        "SET #status = :outcome, status_reason = :reason, service_execution = :execution, " +
        "updated_at = :now, #ttl = :ttl",
      ConditionExpression: "#status = :running",
      ExpressionAttributeNames: names,
      ExpressionAttributeValues: {
        ":outcome": { S: "{% $states.input.outcome %}" },
        ":running": { S: jobStatus("RUNNING") },
        ":reason": REASON_VALUE,
        ":execution": SERVICE_EXECUTION_VALUE,
        ":now": { N: NOW },
        ":ttl": { N: ttl(recordRetention) },
      },
    },
    outputs: "{% $states.input %}",
  });
  finishRunning.addCatch(finishStopped, {
    errors: [TRANSACTION_CANCELED],
    outputs: "{% $states.input %}",
  });
  finishRunning.addCatch(notFinished, { errors: [Errors.ALL] });
  finishRunning.next(requestIngestion);

  // RunJob's catch can't shape its output (a custom state's catch renders no Output), so the
  // failure's outcome and Batch detail are read here from Step Functions' error output.
  const failedRun = `{'outcome': '${jobStatus("FAILED")}', 'run': ${FAILED_RUN}}`;
  const readConnector = sdkCall(scope, "ReadConnector", {
    ...dynamodb,
    iamResources: [connectorsTable.tableArn],
    action: "getItem",
    arguments: {
      TableName: connectorsTable.tableName,
      Key: keys("connectors"),
      ProjectionExpression: "source_access, log_destination",
      ConsistentRead: true,
    },
    outputs: `{% $merge([${failedRun}, {'connector': $states.result.Item}]) %}`,
  });
  readConnector
    .addCatch(finishRunning, { errors: [Errors.ALL], outputs: `{% ${failedRun} %}` })
    .next(finishRunning);

  // The one custom state: CDK's BatchSubmitJob sends ContainerOverrides, which Batch rejects for a
  // job definition with ecsProperties, and has no EcsPropertiesOverride.
  const jobIdVariable = [{ Name: contract.variables.jobId, Value: `{% ${input("job_id")} %}` }];
  const runJob = new CustomState(scope, "RunJob", {
    stateJson: {
      Type: "Task",
      QueryLanguage: "JSONata",
      Resource: `arn:${Aws.PARTITION}:states:::batch:submitJob.sync`,
      Arguments: {
        JobName: `{% ${input("job_id")} %}`,
        JobQueue: jobQueueArn,
        JobDefinition: `{% '${JobExecutionName.DEFINITION_PREFIX}' & ${input("destination_id")} & '-' & ${input("connector_id")} %}`,
        EcsPropertiesOverride: {
          TaskProperties: [
            {
              Containers: [contract.containers.connector, contract.containers.logRouter].map(
                (name) => ({
                  Name: name,
                  Environment: jobIdVariable,
                }),
              ),
            },
          ],
        },
        Tags: { [BATCH_OWNERSHIP_TAG.key]: BATCH_OWNERSHIP_TAG.value },
      },
      Output: `{% {'outcome': '${SUCCEEDED}', 'run': ${runOf("$states.result")}} %}`,
    },
  });
  runJob.addRetry({ errors: [BATCH_THROTTLED], ...RETRY_POLICY });
  runJob.addCatch(readConnector, { errors: [Errors.ALL] });
  runJob.next(finishRunning);

  return sdkCall(scope, "MarkRunning", {
    ...dynamodb,
    iamResources: [jobsTable.tableArn],
    action: "updateItem",
    arguments: {
      TableName: jobsTable.tableName,
      Key: keys("jobs"),
      UpdateExpression: "SET #status = :running, updated_at = :now, #ttl = :ttl",
      ConditionExpression: "#status = :started",
      ExpressionAttributeNames: names,
      ExpressionAttributeValues: {
        ":running": { S: jobStatus("RUNNING") },
        ":started": { S: jobStatus("STARTED") },
        ":now": { N: NOW },
        ":ttl": { N: ttl(activeRecordRetention) },
      },
    },
    notFound: { errors: [CONDITION_FAILED], next: finishStopped },
  }).next(runJob);
};
