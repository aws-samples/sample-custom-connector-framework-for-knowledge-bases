import { Aws } from "aws-cdk-lib";
import { FrameworkExport, importFrameworkValue } from "../common/framework-exports";
import { FrameworkApi } from "./connector-ops-provider";

/**
 * `https://<rest api id>.execute-api.<region>.<suffix>/<stage>`, as the framework stack outputs it,
 * with each partition's DNS suffix: amazonaws.com, amazonaws.com.cn for aws-cn, amazonaws.eu for aws-eusc.
 */
export const FRAMEWORK_ENDPOINT_PATTERN =
  /^https:\/\/([a-z0-9]{10})\.execute-api\.((?:[a-z]{2}|eusc)(?:-[a-z]+)+-[0-9]+)\.amazonaws\.(?:com|com\.cn|eu)\/[A-Za-z0-9_-]+\/?$/;
const ACCOUNT_PATTERN = /^[0-9]{12}$/;
const PARTITION = "(?:aws|aws-us-gov|aws-cn|aws-eusc)";

/** The four values the framework stack outputs for the stacks that use it, as literals. */
export interface FrameworkValuesLiterals {
  readonly endpoint: string;
  readonly serviceAccount: string;
  readonly batchExecutionRoleArn: string;
  readonly destinationWorkflowArn: string;
}

/** Why the literals can't name a framework, naming the field; undefined when they can. */
export const frameworkLiteralsProblem = (literals: FrameworkValuesLiterals): string | undefined => {
  const { endpoint, serviceAccount, batchExecutionRoleArn, destinationWorkflowArn } = literals;
  if (!FRAMEWORK_ENDPOINT_PATTERN.test(endpoint))
    return `endpoint "${endpoint}" must have the form https://<rest api id>.execute-api.<region>.<suffix>/<stage>.`;
  if (!ACCOUNT_PATTERN.test(serviceAccount))
    return `serviceAccount "${serviceAccount}" must be a 12-digit account ID.`;
  if (!new RegExp(`^arn:${PARTITION}:iam::${serviceAccount}:role/.+$`).test(batchExecutionRoleArn))
    return `batchExecutionRoleArn "${batchExecutionRoleArn}" must be an IAM role ARN in the service account.`;
  const workflow = `^arn:${PARTITION}:states:[a-z0-9-]+:${serviceAccount}:stateMachine:[A-Za-z0-9_-]+$`;
  if (!new RegExp(workflow).test(destinationWorkflowArn))
    return `destinationWorkflowArn "${destinationWorkflowArn}" must be a state machine ARN in the service account.`;
  return undefined;
};

interface FrameworkValuesRequest {
  readonly literals: FrameworkValuesLiterals;
  readonly api: FrameworkApi;
  readonly inServiceAccount: boolean;
}

/**
 * The framework values a destination or connector stack needs: the API endpoint, the service
 * account, the Batch execution role ARN, and the destination workflow ARN. A stack in the service
 * account and Region reads them from the framework's exports; a stack in another account takes the
 * literals from its configuration.
 */
export class FrameworkValues {
  public readonly endpoint: string;
  public readonly serviceAccount: string;
  public readonly batchExecutionRoleArn: string;
  public readonly destinationWorkflowArn: string;
  /** True for the exports: the stack is in the service account, so it can use the service's alarm topic. */
  public readonly inServiceAccount: boolean;
  private readonly api: FrameworkApi;

  private constructor({ literals, api, inServiceAccount }: FrameworkValuesRequest) {
    this.endpoint = literals.endpoint;
    this.serviceAccount = literals.serviceAccount;
    this.batchExecutionRoleArn = literals.batchExecutionRoleArn;
    this.destinationWorkflowArn = literals.destinationWorkflowArn;
    this.api = api;
    this.inServiceAccount = inServiceAccount;
  }

  /** The configuration's literals, or the framework's exports when it has none; fails synthesis on bad literals. */
  public static fromConfig(literals?: FrameworkValuesLiterals): FrameworkValues {
    return literals === undefined
      ? FrameworkValues.fromExports()
      : FrameworkValues.fromLiterals(literals);
  }

  private static fromExports(): FrameworkValues {
    const literals: FrameworkValuesLiterals = {
      endpoint: importFrameworkValue(FrameworkExport.API_ENDPOINT),
      serviceAccount: importFrameworkValue(FrameworkExport.SERVICE_ACCOUNT_ID),
      batchExecutionRoleArn: importFrameworkValue(FrameworkExport.BATCH_EXECUTION_ROLE_ARN),
      destinationWorkflowArn: importFrameworkValue(FrameworkExport.DESTINATION_WORKFLOW_ARN),
    };
    const api = {
      endpoint: literals.endpoint,
      restApiId: importFrameworkValue(FrameworkExport.REST_API_ID),
      region: Aws.REGION,
      account: literals.serviceAccount,
    };
    return new FrameworkValues({ literals, api, inServiceAccount: true });
  }

  private static fromLiterals(literals: FrameworkValuesLiterals): FrameworkValues {
    const problem = frameworkLiteralsProblem(literals);
    if (problem !== undefined) throw new Error(problem);
    const [, restApiId, region] = FRAMEWORK_ENDPOINT_PATTERN.exec(literals.endpoint) ?? [];
    const api = {
      endpoint: literals.endpoint,
      restApiId,
      region,
      account: literals.serviceAccount,
    };
    return new FrameworkValues({ literals, api, inServiceAccount: false });
  }

  /** Where the framework API is, for the custom resource providers that call it. */
  public frameworkApi(): FrameworkApi {
    return this.api;
  }
}
