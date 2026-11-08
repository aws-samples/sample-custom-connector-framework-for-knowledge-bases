import { Duration } from "aws-cdk-lib";
import { ScheduleExpression } from "aws-cdk-lib/aws-scheduler";
import { CustomConnectorProjectImageProps } from "custom-connector-framework-cdk";
import { GitHubConnectorAppConfig, OwnerConnectorConfig } from "./app-config";
import { REPOSITORY_ROOT, connectorPathFromRepositoryRoot } from "./source-paths";

/** Every owner's custom connector runs this one image, built from the repository root. */
export const GITHUB_CONNECTOR_IMAGE = {
  directory: REPOSITORY_ROOT,
  file: connectorPathFromRepositoryRoot("backend/Dockerfile"),
  /** The framework construct's build bound before it became a prop. */
  buildTimeout: Duration.hours(1),
} satisfies Omit<CustomConnectorProjectImageProps, "repositoryName" | "maxImageAge">;

/** How long a custom connector's delete waits for the framework's connector deletion workflow; the framework construct's bound before it became a prop. */
export const CONNECTOR_DELETION_TIMEOUT = Duration.hours(1);

/** `github-<owner>`: the custom connector name, and the suffix of its role and log group names. */
export const createConnectorName = (owner: OwnerConnectorConfig): string =>
  `github-${owner.owner.toLowerCase()}`;

export interface ConnectorConfigurationRequest {
  readonly config: GitHubConnectorAppConfig;
  readonly owner: OwnerConnectorConfig;
}

/** The configuration string the job reads: the owner, its repositories, and the configured block. */
export const createConnectorConfiguration = ({
  config,
  owner,
}: ConnectorConfigurationRequest): string =>
  JSON.stringify({
    owner: owner.owner,
    repositories: owner.repositories,
    ...config.connectorConfiguration,
  });

/** A weekly UTC schedule on the owner's configured day and hour. */
export const createConnectorSchedule = (owner: OwnerConnectorConfig): ScheduleExpression =>
  ScheduleExpression.cron({
    weekDay: owner.scheduleWeekDay,
    hour: String(owner.scheduleHourUtc),
    minute: "0",
  });
