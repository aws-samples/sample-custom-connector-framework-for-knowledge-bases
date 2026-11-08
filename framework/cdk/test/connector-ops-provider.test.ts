import { Duration, Stack } from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { RetentionDays } from "aws-cdk-lib/aws-logs";
import {
  ConnectorOpsModule,
  ConnectorOpsProvider,
  ConnectorOpsProviderProps,
  frameworkApiEnvironment,
} from "../lib/constructs/connector-ops-provider";
import { literalFramework, propertiesOf, resourcesOf, tenantStack } from "./construct-fixtures";

const props = (overrides: Partial<ConnectorOpsProviderProps> = {}): ConnectorOpsProviderProps => ({
  module: ConnectorOpsModule.REGISTER,
  environment: frameworkApiEnvironment(literalFramework().frameworkApi()),
  logRetention: RetentionDays.ONE_MONTH,
  totalTimeout: Duration.hours(1),
  ...overrides,
});

const providerIn = (stack: Stack, overrides: Partial<ConnectorOpsProviderProps> = {}) =>
  ConnectorOpsProvider.of({ scope: stack, props: props(overrides) });

describe("ConnectorOpsProvider", () => {
  it("runs every function it creates on arm64, the Provider framework's own included", () => {
    const stack = tenantStack();
    for (const module of Object.values(ConnectorOpsModule)) providerIn(stack, { module });
    const architectures = resourcesOf(Template.fromStack(stack), "AWS::Lambda::Function").map(
      (fn) => JSON.stringify(propertiesOf(fn).Architectures),
    );

    expect(architectures.length).toBeGreaterThan(0);
    expect(new Set(architectures)).toEqual(new Set([JSON.stringify(["arm64"])]));
  });

  it("is one provider per module in a stack", () => {
    const stack = tenantStack();

    expect(providerIn(stack)).toBe(providerIn(stack));
    expect(providerIn(stack, { module: ConnectorOpsModule.DESTINATION })).not.toBe(
      providerIn(stack),
    );
  });

  it("fails synthesis when a second caller asks for another log retention", () => {
    const stack = tenantStack();
    providerIn(stack);

    expect(() => providerIn(stack, { logRetention: RetentionDays.ONE_WEEK })).toThrow(
      /must use the same framework environment and logRetention/,
    );
  });

  it("fails synthesis when a second caller asks for another environment", () => {
    const stack = tenantStack();
    providerIn(stack);

    expect(() =>
      providerIn(stack, {
        environment: { FRAMEWORK_ENDPOINT: "https://other", SERVICE_REGION: "eu-west-1" },
      }),
    ).toThrow(/must use the same framework environment and logRetention/);
  });
});
