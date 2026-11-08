import * as path from "node:path";
import { Aws, Duration, RemovalPolicy, Stack } from "aws-cdk-lib";
import { PolicyStatement } from "aws-cdk-lib/aws-iam";
import {
  ApplicationLogLevel,
  CfnFunction,
  Code,
  Function as LambdaFn,
  Runtime,
} from "aws-cdk-lib/aws-lambda";
import { LogGroup, RetentionDays } from "aws-cdk-lib/aws-logs";
import { Provider } from "aws-cdk-lib/custom-resources";
import { Construct } from "constructs";
import { ApiRoute } from "../common/contracts";
import {
  acknowledgeNagRules,
  CONNECTOR_OPS_ACKNOWLEDGMENTS,
  CONNECTOR_OPS_WILDCARD_REASON,
  resourceFindings,
} from "../common/nag-suppressions";
import {
  LAMBDA_ARCHITECTURE,
  LAMBDA_BUNDLE_ENVIRONMENT,
  LAMBDA_BUNDLE_IMAGE,
  LAMBDA_BUNDLE_PLATFORM,
} from "../construct-groups/backend-bundle";

export type { ApiRoute } from "../common/contracts";

/** The framework directory: the bundle reads api/, toolkit/, and cdk/lambdas/ from it. */
const FRAMEWORK_ROOT = path.join(__dirname, "..", "..", "..");
const ASSET_EXCLUDES =
  ".git,.kiro,backend,docs,cdk/node_modules,cdk/cdk.out,cdk/lib,cdk/test,api/generated,toolkit/tests,**/.mypy_cache,**/.ruff_cache,**/.pytest_cache,**/.venv,**/node_modules,**/__pycache__".split(
    ",",
  );
const TOOLKIT_PACKAGE = "/asset-input/toolkit/src/custom_connector_framework";
const LAMBDA_SOURCE = "/asset-input/cdk/lambdas/connector_ops";
/** Installs the locked dependencies and copies only the API models, the toolkit's client, and the `connector_ops` modules. */
export const CONNECTOR_OPS_BUNDLE_COMMAND = [
  "uv export --quiet --project /asset-input/cdk --frozen --no-dev --no-emit-project --no-emit-local --output-file /tmp/requirements.txt",
  "uv pip install --quiet --python python --target /asset-output --require-hashes --no-deps --no-cache -r /tmp/requirements.txt",
  "cp -R /asset-input/api/packages/ccf-api-models/src/ccf_api_models /asset-output/",
  "mkdir -p /asset-output/custom_connector_framework",
  `cp -R ${TOOLKIT_PACKAGE}/client /asset-output/custom_connector_framework/`,
  `cp ${LAMBDA_SOURCE}/runtime_package_init.py /asset-output/custom_connector_framework/__init__.py`,
  `cp ${LAMBDA_SOURCE}/ccf.py ${LAMBDA_SOURCE}/custom_resource.py ${LAMBDA_SOURCE}/register.py ${LAMBDA_SOURCE}/destination.py ${LAMBDA_SOURCE}/image_build.py /asset-output/`,
  // Each handler module builds its client at import, so the check sets placeholder settings; no call is made.
  "FRAMEWORK_ENDPOINT=https://abcdef1234.execute-api.us-east-1.amazonaws.com/ccf SERVICE_REGION=us-east-1 AWS_DEFAULT_REGION=us-east-1 PYTHONPATH=/asset-output python -c 'import ccf_api_models, custom_connector_framework.client, register, destination, image_build'",
].join(" && ");

/** The CDK Provider framework's own functions, which it builds without an architecture. */
export const PROVIDER_FRAMEWORK_FUNCTION_IDS = [
  "framework-onEvent",
  "framework-isComplete",
  "framework-onTimeout",
];
/** How often the Provider framework calls `isComplete`. */
const QUERY_INTERVAL = Duration.seconds(30);
const HANDLER_TIMEOUT = Duration.minutes(5);

/** The Python module in `lambdas/connector_ops` that serves one custom resource type. */
export enum ConnectorOpsModule {
  REGISTER = "register",
  DESTINATION = "destination",
  IMAGE_BUILD = "image_build",
}

/** Each module's provider construct ID in its stack; a changed ID would change every resource's service token. */
const PROVIDER_IDS: Readonly<Record<ConnectorOpsModule, string>> = {
  [ConnectorOpsModule.REGISTER]: "CcfConnectorRegistrationProvider",
  [ConnectorOpsModule.DESTINATION]: "CcfDestinationProvider",
  [ConnectorOpsModule.IMAGE_BUILD]: "CcfImageBuildProvider",
};

/** The one bundle every `connector_ops` Lambda runs: the locked dependencies and the handler modules. */
const connectorOpsCode = (): Code =>
  Code.fromAsset(FRAMEWORK_ROOT, {
    exclude: ASSET_EXCLUDES,
    bundling: {
      image: LAMBDA_BUNDLE_IMAGE,
      platform: LAMBDA_BUNDLE_PLATFORM,
      environment: LAMBDA_BUNDLE_ENVIRONMENT,
      command: ["bash", "-c", CONNECTOR_OPS_BUNDLE_COMMAND],
    },
  });

/** Where the framework API is: its endpoint, and the REST API ID, Region, and account its ARNs use. */
export interface FrameworkApi {
  readonly endpoint: string;
  readonly restApiId: string;
  readonly region: string;
  readonly account: string;
}

/** The handlers' settings when they call the framework API. */
export const frameworkApiEnvironment = (api: FrameworkApi): Readonly<Record<string, string>> => ({
  FRAMEWORK_ENDPOINT: api.endpoint,
  SERVICE_REGION: api.region,
});

export interface ConnectorOpsProviderProps {
  readonly module: ConnectorOpsModule;
  /** The handlers' environment: `frameworkApiEnvironment(api)`, or none. */
  readonly environment: Readonly<Record<string, string>>;
  readonly logRetention: RetentionDays;
  /** How long `isComplete` may keep returning false before the resource fails. */
  readonly totalTimeout: Duration;
}

/** The construct whose stack holds the provider, and how the provider is built. */
export interface ConnectorOpsProviderRequest {
  readonly scope: Construct;
  readonly props: ConnectorOpsProviderProps;
}

/** The framework API routes both handlers may call. */
export interface AllowRoutesRequest {
  readonly routes: readonly ApiRoute[];
  readonly api: FrameworkApi;
}

/**
 * A CDK `Provider` whose `onEvent` and `isComplete` Lambdas run one `connector_ops` module. Every
 * function it creates runs on arm64, and every log group it creates keeps its records for `logRetention`.
 */
export class ConnectorOpsProvider extends Construct {
  public readonly serviceToken: string;
  public readonly onEvent: LambdaFn;
  public readonly isComplete: LambdaFn;
  private readonly settings: string;

  /**
   * The stack's provider for the module, created on first use. Every construct in a stack shares
   * it, so one that asks for another environment or log retention fails synthesis instead of
   * silently getting the first one's.
   */
  public static of({ scope, props }: ConnectorOpsProviderRequest): ConnectorOpsProvider {
    const stack = Stack.of(scope);
    const id = PROVIDER_IDS[props.module];
    const existing = stack.node.tryFindChild(id);
    if (existing === undefined) return new ConnectorOpsProvider(stack, id, props);
    if (!(existing instanceof ConnectorOpsProvider))
      throw new Error(`${id} in stack ${stack.stackName} isn't a ConnectorOpsProvider.`);
    if (existing.settings !== ConnectorOpsProvider.settingsOf(props))
      throw new Error(
        `Every ${props.module} custom resource in stack ${stack.stackName} must use the same framework ` +
          "environment and logRetention; put the ones that differ in another stack.",
      );
    return existing;
  }

  private static settingsOf({ environment, logRetention }: ConnectorOpsProviderProps): string {
    return JSON.stringify({ environment, logRetention });
  }

  private constructor(scope: Construct, id: string, props: ConnectorOpsProviderProps) {
    super(scope, id);
    const { module, environment, logRetention, totalTimeout } = props;
    this.settings = ConnectorOpsProvider.settingsOf(props);
    const code = connectorOpsCode();
    const logGroup = (logId: string): LogGroup =>
      new LogGroup(this, logId, { retention: logRetention, removalPolicy: RemovalPolicy.DESTROY });
    const handler = (entry: string): LambdaFn =>
      new LambdaFn(this, `${entry}Fn`, {
        runtime: Runtime.PYTHON_3_14,
        architecture: LAMBDA_ARCHITECTURE,
        handler: `${module}.${entry}`,
        code,
        timeout: HANDLER_TIMEOUT,
        environment: { ...environment },
        logGroup: logGroup(`${entry}FnLogs`),
      });
    this.onEvent = handler("on_event");
    this.isComplete = handler("is_complete");
    const provider = new Provider(this, "Provider", {
      onEventHandler: this.onEvent,
      isCompleteHandler: this.isComplete,
      queryInterval: QUERY_INTERVAL,
      totalTimeout,
      frameworkLambdaLoggingLevel: ApplicationLogLevel.ERROR,
      logGroup: logGroup("ProviderLogs"),
      waiterStateMachineLogOptions: { destination: logGroup("WaiterLogs") },
    });
    const frameworkFunctions = PROVIDER_FRAMEWORK_FUNCTION_IDS.map((frameworkFunctionId) => {
      const frameworkFunction = provider.node.findChild(frameworkFunctionId);
      const cfnFunction = frameworkFunction.node.defaultChild;
      if (!(frameworkFunction instanceof LambdaFn) || !(cfnFunction instanceof CfnFunction))
        throw new Error(`The CDK Provider framework no longer creates ${frameworkFunctionId}.`);
      cfnFunction.architectures = [LAMBDA_ARCHITECTURE.name];
      return frameworkFunction;
    });
    this.serviceToken = provider.serviceToken;
    // The Provider framework's roles invoke each handler's versions, and its waiter writes logs through actions on *.
    const invokedVersions = [this.onEvent, this.isComplete, ...frameworkFunctions].map(
      (fn) => `${fn.functionArn}:*`,
    );
    acknowledgeNagRules({
      scopes: [this],
      acknowledgments: [
        ...CONNECTOR_OPS_ACKNOWLEDGMENTS,
        {
          id: "AwsSolutions-IAM5",
          reason: CONNECTOR_OPS_WILDCARD_REASON,
          findings: [
            "Resource::*",
            ...resourceFindings({ scope: this, resources: invokedVersions }),
          ],
        },
      ],
    });
  }

  public allowRoutes({ routes, api }: AllowRoutesRequest): void {
    const routeArns = routes.map(
      (route) =>
        `arn:${Aws.PARTITION}:execute-api:${api.region}:${api.account}:${api.restApiId}/*/${route.method}${route.path}`,
    );
    const statement = new PolicyStatement({
      actions: ["execute-api:Invoke"],
      resources: routeArns,
    });
    for (const handler of [this.onEvent, this.isComplete]) handler.addToRolePolicy(statement);
    acknowledgeNagRules({
      scopes: [this],
      acknowledgments: [
        {
          id: "AwsSolutions-IAM5",
          reason: CONNECTOR_OPS_WILDCARD_REASON,
          findings: resourceFindings({ scope: this, resources: routeArns }),
        },
      ],
    });
  }
}
