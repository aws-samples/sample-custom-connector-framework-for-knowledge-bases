import { App, Tags, Validations } from "aws-cdk-lib";
import { AwsSolutionsChecks } from "cdk-nag";
import { ConnectorStackName, GitHubConnectorAppConfig } from "./common/app-config";
import { GitHubCustomConnectorStack } from "./stacks/github-custom-connector-stack";

export interface GitHubConnectorAppRequest {
  readonly app: App;
  readonly config: GitHubConnectorAppConfig;
  readonly account: string;
  readonly region: string;
}

/**
 * Add the one `GitHubCustomConnector` stack to the app, built only from the framework's
 * constructs. It references an existing destination by ID and never creates one.
 */
export const buildGitHubConnectorApp = (
  request: GitHubConnectorAppRequest,
): GitHubCustomConnectorStack => {
  const { app, config, account, region } = request;
  const connector = new GitHubCustomConnectorStack(app, ConnectorStackName.CONNECTOR, {
    stackName: ConnectorStackName.CONNECTOR,
    description:
      "The GitHub connector image, the GitHub token secret, and one custom connector per GitHub owner.",
    config,
    env: { account, region },
  });
  Tags.of(connector).add("project", "github-custom-connector");
  Tags.of(connector).add("cost-center", config.costCenter);
  Tags.of(connector).add("team", config.team);
  Validations.of(app).addPlugins(new AwsSolutionsChecks(app, { verbose: true }));
  return connector;
};
