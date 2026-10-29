import * as path from "node:path";
import { App, Aspects, CliCredentialsStackSynthesizer, Duration } from "aws-cdk-lib";
import { ScheduleExpression } from "aws-cdk-lib/aws-scheduler";
import { AwsSolutionsChecks } from "cdk-nag";
import { applySampleNagSuppressions } from "./common/sample-nag-suppressions";
import { ClientImageAssetsFoundationStack } from "./stacks/client-image-assets-foundation-stack";
import {
  ConnectorConfiguration,
  ConnectorImageSource,
  CustomConnectorFrameworkClientStack,
} from "./stacks/custom-connector-framework-client-stack";

// All three samples share one image; CCF picks the definition through the command.
const SAMPLE_CONNECTOR_IMAGE = {
  directory: path.join(__dirname, "..", ".."),
  file: "samples/Dockerfile",
} satisfies ConnectorImageSource;

export enum SampleName {
  WEB_CRAWLER = "web-crawler",
  GITLAB = "gitlab",
  GITHUB = "github",
}

type WebCrawlerConnectorConfiguration = {
  readonly urls: readonly { readonly url: string }[];
};

type GitHubConnectorConfiguration = {
  readonly token_secret_arn: string;
  readonly repositories: readonly { readonly full_name: string }[];
  readonly api_url?: string;
};

type GitLabConnectorConfiguration = {
  readonly token_secret_arn: string;
  readonly projects: readonly { readonly project_id: string }[];
  readonly server_url?: string;
};

interface ResolvedConnectorProperties {
  readonly connectorConfiguration: ConnectorConfiguration;
  readonly sourceSecretArns: readonly string[];
}

interface SampleConnectorDefinition {
  readonly stackId: "SampleWebCrawlerStack" | "SampleGitHubStack" | "SampleGitLabStack";
  readonly command: readonly ["ccf-connector", "run", string];
  readonly resolveProperties: (app: App) => ResolvedConnectorProperties;
}

const DAILY_SCHEDULE = ScheduleExpression.rate(Duration.days(1));

const optionalContext = (app: App, key: string): string | undefined => {
  const contextValue: unknown = app.node.tryGetContext(key);
  if (contextValue === undefined) return undefined;
  if (typeof contextValue !== "string" || contextValue.length === 0)
    throw new Error(`Context value "${key}" must be a non-empty string.`);
  return contextValue;
};

const requiredContext = (app: App, key: string): string => {
  const contextValue = optionalContext(app, key);
  if (contextValue === undefined)
    throw new Error(`Missing required context value "${key}" for a sample connector synth.`);
  return contextValue;
};

const resolveGitHubProperties = (app: App): ResolvedConnectorProperties => {
  const tokenSecretArn = requiredContext(app, "tokenSecretArn");
  const apiUrl = optionalContext(app, "githubApiUrl");
  return {
    connectorConfiguration: {
      token_secret_arn: tokenSecretArn,
      repositories: [{ full_name: requiredContext(app, "repositoryFullName") }],
      ...(apiUrl === undefined ? {} : { api_url: apiUrl }),
    } satisfies GitHubConnectorConfiguration,
    sourceSecretArns: [tokenSecretArn],
  };
};

const resolveGitLabProperties = (app: App): ResolvedConnectorProperties => {
  const tokenSecretArn = requiredContext(app, "tokenSecretArn");
  const serverUrl = optionalContext(app, "gitlabServerUrl");
  return {
    connectorConfiguration: {
      token_secret_arn: tokenSecretArn,
      projects: [{ project_id: requiredContext(app, "projectId") }],
      ...(serverUrl === undefined ? {} : { server_url: serverUrl }),
    } satisfies GitLabConnectorConfiguration,
    sourceSecretArns: [tokenSecretArn],
  };
};

export const SAMPLE_CONNECTOR_DEFINITIONS = {
  [SampleName.WEB_CRAWLER]: {
    stackId: "SampleWebCrawlerStack",
    command: ["ccf-connector", "run", "web_crawler.connector:definition"],
    resolveProperties: () => ({
      connectorConfiguration: {
        urls: [{ url: "https://example.com" }],
      } satisfies WebCrawlerConnectorConfiguration,
      sourceSecretArns: [],
    }),
  },
  [SampleName.GITHUB]: {
    stackId: "SampleGitHubStack",
    command: ["ccf-connector", "run", "github_connector.connector:definition"],
    resolveProperties: resolveGitHubProperties,
  },
  [SampleName.GITLAB]: {
    stackId: "SampleGitLabStack",
    command: ["ccf-connector", "run", "gitlab_connector.connector:definition"],
    resolveProperties: resolveGitLabProperties,
  },
} satisfies Record<SampleName, SampleConnectorDefinition>;

const resolveSampleName = (sample: string): SampleName => {
  const normalizedSample = sample.toLowerCase();
  for (const sampleName of Object.values(SampleName))
    if (sampleName === normalizedSample) return sampleName;
  throw new Error(`Invalid sample "${sample}". Expected: ${Object.values(SampleName).join(", ")}.`);
};

/** Synthesize the client-owned repository foundation and one selected sample stack. */
export function synthSampleStack(app: App, account: string | undefined, region: string): boolean {
  const requestedSample = optionalContext(app, "sample");
  if (requestedSample === undefined) return false;
  const sampleName = resolveSampleName(requestedSample);
  const definition = SAMPLE_CONNECTOR_DEFINITIONS[sampleName];
  const frameworkExecutionPrincipalArn = requiredContext(app, "frameworkExecutionPrincipalArn");
  const env = { account, region };
  const foundation = new ClientImageAssetsFoundationStack(app, "ClientImageAssetsFoundationStack", {
    frameworkExecutionPrincipalArn,
    env,
  });
  const resolvedProperties = definition.resolveProperties(app);
  const sampleStack = new CustomConnectorFrameworkClientStack(app, definition.stackId, {
    serviceReference: {
      endpoint: requiredContext(app, "apiEndpoint"),
      region: optionalContext(app, "frameworkRegion") ?? region,
      serviceAccountId: requiredContext(app, "frameworkAccount"),
      frameworkExecutionPrincipalArn,
      restApiId: requiredContext(app, "frameworkRestApiId"),
    },
    connectorName: sampleName,
    connectorImage: SAMPLE_CONNECTOR_IMAGE,
    command: definition.command,
    connectorConfiguration: resolvedProperties.connectorConfiguration,
    sourceAccessExternalId: requiredContext(app, "sourceAccessExternalId"),
    sourceSecretArns: resolvedProperties.sourceSecretArns,
    schedule: DAILY_SCHEDULE,
    synthesizer: new CliCredentialsStackSynthesizer({
      imageAssetsRepositoryName: foundation.repositoryName,
    }),
    env,
  });
  sampleStack.addStackDependency(
    foundation,
    "The dedicated repository must exist before CDK publishes the sample image asset.",
  );
  applySampleNagSuppressions(sampleStack);
  Aspects.of(sampleStack).add(new AwsSolutionsChecks({ verbose: true }));
  return true;
}
