import { Annotations } from "aws-cdk-lib";
import { CustomConnector, CustomConnectorProject } from "custom-connector-framework-cdk";
import { Construct } from "constructs";
import { GitHubConnectorAppConfig } from "../common/app-config";
import {
  CONNECTOR_DELETION_TIMEOUT,
  createConnectorConfiguration,
  createConnectorName,
  createConnectorSchedule,
} from "../common/owner-connectors";
import { GitHubTokenSecret } from "./github-token-secret";

export interface GitHubCustomConnectorsProps {
  readonly config: GitHubConnectorAppConfig;
  /** The project's image, destination, and framework values every owner's custom connector uses. */
  readonly project: CustomConnectorProject;
}

/** One GitHub owner and the custom connector that reads it. */
export interface GitHubOwnerConnector {
  readonly owner: string;
  readonly name: string;
  readonly connector: CustomConnector;
}

/**
 * The GitHub-specific resources: the token secret, and one `CustomConnector` per owner the sources
 * name, each granted read access to the secret. A `REPOSITORY` source that an `ORG` source for the
 * same owner already covers gets a synth warning.
 */
export class GitHubCustomConnectors extends Construct {
  public readonly tokenSecret: GitHubTokenSecret;
  public readonly connectors: readonly GitHubOwnerConnector[];

  constructor(scope: Construct, id: string, props: GitHubCustomConnectorsProps) {
    super(scope, id);
    const { config, project } = props;
    this.tokenSecret = new GitHubTokenSecret(this, "GitHubToken", {
      removalPolicy: config.removalPolicy,
    });
    const secret = this.tokenSecret.secret;
    for (const repository of config.redundantRepositories)
      Annotations.of(this).addWarningV2(
        "ccf:redundantRepositorySource",
        `The REPOSITORY source ${repository} is redundant: an ORG source for its owner reads every repository.`,
      );
    this.connectors = config.connectors.map((owner) => {
      const name = createConnectorName(owner);
      const connector = project.addCustomConnector({
        id: `Connector-${owner.owner}`,
        name,
        command: config.command,
        connectorConfiguration: createConnectorConfiguration({ config, owner }),
        secretArn: secret.secretArn,
        schedule: createConnectorSchedule(owner),
        compute: owner.compute,
        sourceAccessServices: config.sourceAccessServices,
        deletionTimeout: CONNECTOR_DELETION_TIMEOUT,
        ...(config.logDelivery
          ? { logDelivery: { retention: config.logRetention, removalPolicy: config.removalPolicy } }
          : {}),
      });
      secret.grantRead(connector);
      return { owner: owner.owner, name, connector };
    });
  }
}
