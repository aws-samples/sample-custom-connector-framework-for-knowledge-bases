import * as path from "node:path";
import {
  ArnFormat,
  CfnOutput,
  CustomResource,
  DockerImage,
  Duration,
  RemovalPolicy,
  Stack,
  StackProps,
  Token,
} from "aws-cdk-lib";
import { DockerImageAsset, Platform } from "aws-cdk-lib/aws-ecr-assets";
import { AccountPrincipal, Effect, PolicyStatement, Role } from "aws-cdk-lib/aws-iam";
import {
  ApplicationLogLevel,
  Architecture,
  Code,
  Function as LambdaFn,
  Runtime,
} from "aws-cdk-lib/aws-lambda";
import { LogGroup, RetentionDays } from "aws-cdk-lib/aws-logs";
import { RegionInfo } from "aws-cdk-lib/region-info";
import { ScheduleExpression } from "aws-cdk-lib/aws-scheduler";
import { Provider } from "aws-cdk-lib/custom-resources";
import { Construct } from "constructs";
import { CcfServiceReference } from "../common/ccf-service-reference";
import { ProjectName, ResourceName, createConstructId, createResourceName } from "../common/config";
import {
  RUNTIME_ROLE_NAME_PREFIX,
  RUNTIME_ROLE_PATH,
} from "../construct-groups/runtime-identities";
const REPO_ROOT = path.join(__dirname, "..", "..", "..");
const DEFAULT_CPU = 1;
const DEFAULT_MEMORY_MIB = 2048;
const DEFAULT_TIMEOUT_SECONDS = 900;
const MAX_SOURCE_ROLE_CONNECTOR_NAME_LENGTH = 48;
const FRAMEWORK_PRINCIPAL_PATTERN =
  /^arn:(aws|aws-cn|aws-us-gov|aws-iso|aws-iso-b|aws-iso-e|aws-iso-f|aws-eusc):iam::(\d{12}):role\/CcfFrameworkExecutionRole$/;
const SOURCE_SECRET_ARN_PATTERN =
  /^arn:(aws|aws-cn|aws-us-gov|aws-iso|aws-iso-b|aws-iso-e|aws-iso-f|aws-eusc):secretsmanager:([a-z]{2,4}(?:-[a-z]+){1,2}-[1-9]\d*):(\d{12}):secret:[A-Za-z0-9/_+=.@-]+-[A-Za-z0-9]{6}$/;
const ASSET_EXCLUDES =
  ".git,.kiro,backend,cdk/node_modules,cdk/cdk.out,docs,samples,**/.mypy_cache,**/.ruff_cache,**/.pytest_cache,**/.venv,**/node_modules,**/__pycache__".split(
    ",",
  );
const DOCKER_IMAGE_ASSET_EXCLUDES =
  "**/.venv,**/node_modules,**/cdk.out,**/.mypy_cache,**/.ruff_cache,**/.pytest_cache,**/__pycache__,**/*.pyc".split(
    ",",
  );
export const REGISTRATION_BUNDLE_PLATFORM = "linux/amd64";
export const REGISTRATION_BUNDLE_COMMAND = [
  "uv export --quiet --project /asset-input/cdk --frozen --no-dev --no-emit-project --no-emit-local --prune filetype --output-file /tmp/requirements.txt",
  "uv pip install --quiet --python /usr/local/bin/python --target /asset-output --require-hashes --no-deps --no-cache -r /tmp/requirements.txt",
  "cp -R /asset-input/api/packages/ccf-api-models/src/ccf_api_models /asset-output/",
  "mkdir -p /asset-output/custom_connector_framework",
  "cp /asset-input/toolkit/src/custom_connector_framework/client.py /asset-input/toolkit/src/custom_connector_framework/_generated_client.py /asset-output/custom_connector_framework/",
  "cp /asset-input/cdk/lambdas/connector_ops/runtime_package_init.py /asset-output/custom_connector_framework/__init__.py",
  "cp /asset-input/cdk/lambdas/connector_ops/ccf.py /asset-input/cdk/lambdas/connector_ops/register.py /asset-output/",
  "PYTHONPATH=/asset-output python -c 'import ccf_api_models, custom_connector_framework.client, register'",
].join(" && ");
interface ArnCaptureIndexes {
  readonly partition: number;
  readonly region?: number;
  readonly account: number;
}
interface ArnExpectation {
  readonly value: string;
  readonly pattern: RegExp;
  readonly captureIndexes: ArnCaptureIndexes;
  readonly account: string;
  readonly targetRegion: string;
}
const validateResolvedArn = ({
  value,
  pattern,
  captureIndexes,
  account,
  targetRegion,
}: ArnExpectation): void => {
  if (Token.isUnresolved(value)) return;
  const match = pattern.exec(value);
  const accountMismatch =
    match !== null && !Token.isUnresolved(account) && match[captureIndexes.account] !== account;
  const regionMismatch =
    match !== null &&
    captureIndexes.region !== undefined &&
    !Token.isUnresolved(targetRegion) &&
    match[captureIndexes.region] !== targetRegion;
  const partitionMismatch =
    match !== null &&
    !Token.isUnresolved(targetRegion) &&
    match[captureIndexes.partition] !== RegionInfo.get(targetRegion).partition;
  if (match === null || accountMismatch || regionMismatch || partitionMismatch)
    throw new Error("A client stack ARN is malformed or targets a different account or Region.");
};
export type ConnectorConfigurationValue =
  string | number | boolean | readonly ConnectorConfigurationValue[] | ConnectorConfiguration;
export interface ConnectorConfiguration {
  readonly [propertyName: string]: ConnectorConfigurationValue;
}
/** Where CDK builds the connector container image from. */
export interface ConnectorImageSource {
  /** Build context directory. */
  readonly directory: string;
  /** Dockerfile path, relative to `directory`. */
  readonly file: string;
}
export interface CustomConnectorFrameworkClientStackProps extends StackProps {
  readonly serviceReference: CcfServiceReference;
  readonly connectorName: string;
  readonly connectorImage: ConnectorImageSource;
  readonly command: readonly string[];
  readonly connectorConfiguration: ConnectorConfiguration;
  readonly sourceAccessExternalId: string;
  readonly sourceSecretArns?: readonly string[];
  readonly schedule?: ScheduleExpression;
  readonly cpu?: number;
  readonly memoryMib?: number;
  readonly timeoutSeconds?: number;
}
export class CustomConnectorFrameworkClientStack extends Stack {
  public readonly sourceRole: Role;
  constructor(scope: Construct, id: string, props: CustomConnectorFrameworkClientStackProps) {
    super(scope, id, props);
    const { serviceReference, connectorName, connectorImage, sourceAccessExternalId } = props;
    validateResolvedArn({
      value: serviceReference.frameworkExecutionPrincipalArn,
      pattern: FRAMEWORK_PRINCIPAL_PATTERN,
      captureIndexes: { partition: 1, account: 2 },
      account: serviceReference.serviceAccountId,
      targetRegion: serviceReference.region,
    });
    if (
      !Token.isUnresolved(serviceReference.region) &&
      !Token.isUnresolved(this.region) &&
      serviceReference.region !== this.region
    )
      throw new Error(
        `CCF service Region "${serviceReference.region}" must match client stack Region "${this.region}".`,
      );
    const sourceSecretArns = props.sourceSecretArns ?? [];
    for (const secretArn of sourceSecretArns)
      validateResolvedArn({
        value: secretArn,
        pattern: SOURCE_SECRET_ARN_PATTERN,
        captureIndexes: { partition: 1, region: 2, account: 3 },
        account: this.account,
        targetRegion: this.region,
      });
    if (connectorName.length > MAX_SOURCE_ROLE_CONNECTOR_NAME_LENGTH)
      throw new Error(
        `Connector name must be at most ${MAX_SOURCE_ROLE_CONNECTOR_NAME_LENGTH} characters for deterministic source-role naming.`,
      );
    // Connector containers receive only the service-managed per-connector
    // runtime role, never the Batch execution role. That role's ARN is minted
    // by CCF at registration time, so trust names the service account plus the
    // service-owned runtime-role path and requires this connector's external ID.
    const runtimeRoleArnPattern = `arn:${this.partition}:iam::${serviceReference.serviceAccountId}:role${RUNTIME_ROLE_PATH}${RUNTIME_ROLE_NAME_PREFIX}*`;
    this.sourceRole = new Role(this, createConstructId(ResourceName.SOURCE_ROLE_PREFIX), {
      roleName: `${createResourceName(ProjectName.CCF, ResourceName.SOURCE_ROLE_PREFIX)}-${connectorName}`,
      assumedBy: new AccountPrincipal(serviceReference.serviceAccountId).withConditions({
        StringEquals: { "sts:ExternalId": sourceAccessExternalId },
        ArnLike: { "aws:PrincipalArn": runtimeRoleArnPattern },
      }),
      description: `Source access assumed by the CCF connector runtime role for ${connectorName}.`,
    });
    for (const secretArn of sourceSecretArns)
      this.sourceRole.addToPolicy(
        new PolicyStatement({ actions: ["secretsmanager:GetSecretValue"], resources: [secretArn] }),
      );
    const connectorImageAsset = new DockerImageAsset(this, createConstructId("ConnectorImage"), {
      directory: connectorImage.directory,
      file: connectorImage.file,
      platform: Platform.LINUX_AMD64,
      exclude: DOCKER_IMAGE_ASSET_EXCLUDES,
    });
    const registration = new CustomResource(this, createConstructId("ConnectorRegistration"), {
      serviceToken: this.createRegistrationProvider(serviceReference).serviceToken,
      properties: {
        ConnectorName: connectorName,
        DockerImageUri: connectorImageAsset.imageUri,
        Command: props.command,
        ConnectorConfiguration: props.connectorConfiguration,
        SourceRoleArn: this.sourceRole.roleArn,
        SourceAccessExternalId: sourceAccessExternalId,
        ScheduleExpression: props.schedule?.expressionString,
        Cpu: props.cpu ?? DEFAULT_CPU,
        Memory: props.memoryMib ?? DEFAULT_MEMORY_MIB,
        Timeout: props.timeoutSeconds ?? DEFAULT_TIMEOUT_SECONDS,
      },
    });
    new CfnOutput(this, "ConnectorImageUri", { value: connectorImageAsset.imageUri });
    new CfnOutput(this, "ConnectorId", { value: registration.ref });
  }
  private createRegistrationProvider(serviceReference: CcfServiceReference): Provider {
    const registrationHandler = new LambdaFn(this, createConstructId("ConnectorRegistrationFn"), {
      runtime: Runtime.PYTHON_3_14,
      architecture: Architecture.X86_64,
      handler: "register.handler",
      code: Code.fromAsset(REPO_ROOT, {
        exclude: ASSET_EXCLUDES,
        bundling: {
          image: DockerImage.fromBuild(REPO_ROOT, {
            file: "cdk/lambdas/connector_ops/Dockerfile",
            platform: REGISTRATION_BUNDLE_PLATFORM,
          }),
          environment: { UV_CACHE_DIR: "/tmp/uv-cache", UV_LINK_MODE: "copy" },
          command: ["bash", "-c", REGISTRATION_BUNDLE_COMMAND],
        },
      }),
      timeout: Duration.minutes(5),
      environment: { CCF_ENDPOINT: serviceReference.endpoint },
      logGroup: new LogGroup(this, createConstructId("ConnectorRegistrationFnLogs"), {
        retention: RetentionDays.ONE_MONTH,
        removalPolicy: RemovalPolicy.DESTROY,
      }),
    });
    this.grantRegistrationApiInvoke(registrationHandler, serviceReference);
    return new Provider(this, createConstructId("ConnectorRegistrationProvider"), {
      onEventHandler: registrationHandler,
      frameworkLambdaLoggingLevel: ApplicationLogLevel.ERROR,
    });
  }
  private grantRegistrationApiInvoke(
    registrationHandler: LambdaFn,
    serviceReference: CcfServiceReference,
  ): void {
    const { restApiId, region, serviceAccountId } = serviceReference;
    const registrationRoutes = [
      ["POST", "/api/v1/custom-connectors"],
      ["PUT", "/api/v1/custom-connectors/*"],
      ["DELETE", "/api/v1/custom-connectors/*"],
    ] as const;
    registrationHandler.addToRolePolicy(
      new PolicyStatement({
        effect: Effect.ALLOW,
        actions: ["execute-api:Invoke"],
        resources: registrationRoutes.map(([method, routePath]) =>
          this.formatArn({
            service: "execute-api",
            region,
            account: serviceAccountId,
            resource: restApiId,
            resourceName: `*/${method}${routePath}`,
            arnFormat: ArnFormat.SLASH_RESOURCE_NAME,
          }),
        ),
      }),
    );
  }
}
