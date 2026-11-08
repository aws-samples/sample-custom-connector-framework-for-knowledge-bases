import { CfnOutput, Stack, StackProps } from "aws-cdk-lib";
import {
  CustomConnectorProject,
  Destination,
  FrameworkValues,
} from "custom-connector-framework-cdk";
import { Construct } from "constructs";
import { GitHubConnectorAppConfig } from "../common/app-config";
import { GITHUB_CONNECTOR_IMAGE } from "../common/owner-connectors";
import { GitHubCustomConnectors } from "../construct-groups/github-custom-connectors";

export interface GitHubCustomConnectorStackProps extends StackProps {
  readonly config: GitHubConnectorAppConfig;
}

/**
 * The GitHub connector's one stack: its image, built into its own repository; the GitHub token
 * secret; and one custom connector per configured owner in the configured destination. With the
 * configuration's `framework` values it reads no framework export, so it deploys in any account.
 */
export class GitHubCustomConnectorStack extends Stack {
  public readonly project: CustomConnectorProject;
  public readonly connectors: GitHubCustomConnectors;

  constructor(scope: Construct, id: string, props: GitHubCustomConnectorStackProps) {
    super(scope, id, props);
    const { config } = props;
    const framework = FrameworkValues.fromConfig(config.framework);
    this.project = new CustomConnectorProject(this, "Project", {
      framework,
      destination: Destination.fromDestinationId(this, "Destination", {
        destinationId: config.destination.id,
        framework,
      }),
      image: {
        ...GITHUB_CONNECTOR_IMAGE,
        repositoryName: config.imageRepositoryName,
        maxImageAge: config.maxImageAge,
      },
      removalPolicy: config.removalPolicy,
      logRetention: config.logRetention,
    });
    this.connectors = new GitHubCustomConnectors(this, "GitHub", {
      config,
      project: this.project,
    });
    for (const { owner, name, connector } of this.connectors.connectors)
      new CfnOutput(this, `ConnectorId-${owner}`, {
        description: `The custom connector ID of the ${name} custom connector, for a manual start.`,
        value: connector.connectorId,
      });
    new CfnOutput(this, "GitHubTokenSecretArn", {
      description: "Store the GitHub token the custom connectors use in this secret.",
      value: this.connectors.tokenSecret.secret.secretArn,
    });
  }
}
