import { RemovalPolicy, Validations } from "aws-cdk-lib";
import { Secret } from "aws-cdk-lib/aws-secretsmanager";
import { Construct } from "constructs";

export interface GitHubTokenSecretProps {
  readonly removalPolicy: RemovalPolicy;
}

/**
 * The Secrets Manager secret every owner's custom connector reads its GitHub token from, through
 * its source-access role. CloudFormation creates it with a generated placeholder; the
 * operator stores a real token in it once (see the connector README), and its jobs fail
 * until then.
 */
export class GitHubTokenSecret extends Construct {
  public readonly secret: Secret;

  constructor(scope: Construct, id: string, props: GitHubTokenSecretProps) {
    super(scope, id);
    const { removalPolicy } = props;
    this.secret = new Secret(this, "Secret", {
      description:
        "GitHub token the GitHub custom connectors use for repository search and archive downloads.",
      removalPolicy,
    });
    Validations.of(this.secret).acknowledge({
      id: "AwsSolutions-SMG4",
      reason:
        "A GitHub personal access token cannot be rotated by a Secrets Manager rotation Lambda.",
    });
  }
}
