import { RemovalPolicy, Stack, StackProps } from "aws-cdk-lib";
import { Repository, TagMutability } from "aws-cdk-lib/aws-ecr";
import { ArnPrincipal, PolicyStatement } from "aws-cdk-lib/aws-iam";
import { Construct } from "constructs";
import { createClientImageAssetsRepositoryName } from "../common/client-image-assets-repository";
export interface ClientImageAssetsFoundationStackProps extends StackProps {
  readonly frameworkExecutionPrincipalArn: string;
}
export class ClientImageAssetsFoundationStack extends Stack {
  public readonly repositoryName: string;
  constructor(scope: Construct, id: string, props: ClientImageAssetsFoundationStackProps) {
    super(scope, id, props);
    const { frameworkExecutionPrincipalArn } = props;
    this.repositoryName = createClientImageAssetsRepositoryName({
      account: this.account,
      region: this.region,
    });
    const repository = new Repository(this, "Repository", {
      repositoryName: this.repositoryName,
      imageScanOnPush: true,
      imageTagMutability: TagMutability.IMMUTABLE,
      removalPolicy: RemovalPolicy.RETAIN,
    });
    repository.addToResourcePolicy(
      new PolicyStatement({
        principals: [new ArnPrincipal(frameworkExecutionPrincipalArn)],
        actions: [
          "ecr:BatchCheckLayerAvailability",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
        ],
      }),
    );
  }
}
