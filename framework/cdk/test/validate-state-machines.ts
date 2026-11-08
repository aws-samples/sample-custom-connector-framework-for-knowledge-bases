/**
 * Validates every state machine of `CustomConnectorFramework` with Step Functions' own checker,
 * `aws stepfunctions validate-state-machine-definition`, which is read-only. `make check` runs it.
 * It synthesizes the stack in memory without bundling, so it needs no deployed stack.
 * Without AWS credentials it says so and exits 0; with them, any diagnostic fails it.
 */
import { execFileSync } from "node:child_process";
import { resolve } from "node:path";
import { RemovalPolicy } from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { RetentionDays } from "aws-cdk-lib/aws-logs";
import { loadFrameworkAppConfig } from "../lib/common/app-config";
import { ProjectName } from "../lib/common/config";
import { CustomConnectorFrameworkStack } from "../lib/stacks/custom-connector-framework-stack";
import { createApp } from "./construct-fixtures";

const ACCOUNT = "444455556666";
const REGION = "us-west-2";
/** What each pseudo parameter resolves to; any other reference becomes a placeholder name. */
const PSEUDO_PARAMETERS: Readonly<Record<string, string>> = {
  "AWS::Partition": "aws",
  "AWS::Region": REGION,
  "AWS::AccountId": ACCOUNT,
  "AWS::URLSuffix": "amazonaws.com",
};

interface ValidationResult {
  readonly result: string;
  readonly diagnostics: readonly {
    readonly severity: string;
    readonly code: string;
    readonly message: string;
  }[];
}

const hasCredentials = (): boolean => {
  try {
    execFileSync("aws", ["sts", "get-caller-identity"], { stdio: "ignore" });
    return true;
  } catch {
    return false;
  }
};

const frameworkTemplate = (): Template => {
  const config = loadFrameworkAppConfig({
    path: resolve(__dirname, "..", "config", "example-disposable.json"),
    serviceAccount: ACCOUNT,
  });
  const stack = new CustomConnectorFrameworkStack(createApp(), "CustomConnectorFramework", {
    ...config,
    projectName: ProjectName.CCF,
    removalPolicy: RemovalPolicy.DESTROY,
    logRetention: RetentionDays.ONE_MONTH,
    region: REGION,
    deletionProtectionThreshold: 15,
    env: { account: ACCOUNT, region: REGION },
  });
  return Template.fromStack(stack);
};

/** The definition string with every CloudFormation reference resolved to a literal. */
const resolved = (value: unknown): string => {
  if (typeof value === "string") return value;
  const intrinsic = value as {
    readonly "Fn::Join"?: [string, unknown[]];
    readonly Ref?: string;
    readonly "Fn::GetAtt"?: [string, string];
  };
  if (intrinsic["Fn::Join"] !== undefined) {
    const [separator, parts] = intrinsic["Fn::Join"];
    return parts.map(resolved).join(separator);
  }
  if (intrinsic.Ref !== undefined)
    return PSEUDO_PARAMETERS[intrinsic.Ref] ?? `placeholder-${intrinsic.Ref}`;
  if (intrinsic["Fn::GetAtt"] !== undefined)
    return `placeholder-${intrinsic["Fn::GetAtt"].join("-")}`;
  throw new Error(`Unexpected definition part ${JSON.stringify(value)}`);
};

const validate = (definition: string): ValidationResult =>
  JSON.parse(
    execFileSync(
      "aws",
      [
        "stepfunctions",
        "validate-state-machine-definition",
        "--type",
        "STANDARD",
        "--region",
        REGION,
        "--definition",
        definition,
        "--output",
        "json",
      ],
      { encoding: "utf8" },
    ),
  ) as ValidationResult;

const main = (): number => {
  if (!hasCredentials()) {
    console.log(
      "validate-state-machines: no AWS credentials, so Step Functions' validator was not run.",
    );
    return 0;
  }
  let failed = false;
  const machines = frameworkTemplate().findResources("AWS::StepFunctions::StateMachine");
  for (const machine of Object.values(machines)) {
    const name = String(machine.Properties.StateMachineName);
    const result = validate(resolved(machine.Properties.DefinitionString));
    console.log(`validate-state-machines: ${name}: ${result.result}`);
    for (const diagnostic of result.diagnostics)
      console.log(`  ${diagnostic.severity} ${diagnostic.code}: ${diagnostic.message}`);
    if (result.result !== "OK" || result.diagnostics.length > 0) failed = true;
  }
  return failed ? 1 : 0;
};

process.exit(main());
