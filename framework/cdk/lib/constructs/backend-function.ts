import { Duration, RemovalPolicy } from "aws-cdk-lib";
import { IRole } from "aws-cdk-lib/aws-iam";
import { Code, Function as LambdaFunction, Runtime, Tracing } from "aws-cdk-lib/aws-lambda";
import { LogGroup, RetentionDays } from "aws-cdk-lib/aws-logs";
import { Construct } from "constructs";
import { ProjectName, ResourceName, createResourceName } from "../common/config";
import { BackendBundle, LAMBDA_ARCHITECTURE } from "../construct-groups/backend-bundle";

export interface BackendFunctionProps {
  /** The function's fixed name is `Ccf<name>`. */
  readonly name: ResourceName;
  /** `<module>.handler` in `backend/src`. */
  readonly handler: string;
  readonly bundle: BackendBundle;
  /** The settings the handler's composition root reads; the stack tests check the keys against `settings.json`. */
  readonly environment: Readonly<Record<string, string>>;
  readonly memorySize: number;
  readonly timeout: Duration;
  readonly logRetention: RetentionDays;
  readonly logRemovalPolicy: RemovalPolicy;
  /** A role with a fixed name; unset, CDK creates one. */
  readonly role?: IRole;
  readonly reservedConcurrentExecutions?: number;
}

/** One framework backend Lambda: arm64, traced, from the backend bundle, with its own log group. */
export class BackendFunction extends Construct {
  public readonly function: LambdaFunction;

  constructor(scope: Construct, id: string, props: BackendFunctionProps) {
    super(scope, id);
    const {
      name,
      handler,
      bundle,
      environment,
      memorySize,
      timeout,
      logRetention,
      logRemovalPolicy,
    } = props;
    this.function = new LambdaFunction(this, "Function", {
      functionName: createResourceName({ projectName: ProjectName.CCF, resourceName: name }),
      runtime: Runtime.PYTHON_3_14,
      architecture: LAMBDA_ARCHITECTURE,
      handler,
      code: Code.fromAsset(bundle.sourcePath, { bundling: bundle.bundling }),
      environment: { ...environment },
      role: props.role,
      reservedConcurrentExecutions: props.reservedConcurrentExecutions,
      tracing: Tracing.ACTIVE,
      timeout,
      memorySize,
      logGroup: new LogGroup(this, "Logs", {
        retention: logRetention,
        removalPolicy: logRemovalPolicy,
      }),
    });
  }
}
