import { CustomResource } from "aws-cdk-lib";
import { AccountPrincipal, IManagedPolicy, PolicyDocument, Role } from "aws-cdk-lib/aws-iam";
import { Construct } from "constructs";
import {
  acknowledgeNagRules,
  CONNECTOR_ROLE_WILDCARD_REASON,
  resourceFindings,
} from "../common/nag-suppressions";
import { RegistrationAttribute } from "./custom-connector";

/** A tenant role's inline policy, and the wildcard resources in it that cdk-nag reports. */
export interface TenantAccessPolicy {
  readonly document: PolicyDocument;
  readonly wildcardResources: readonly string[];
}

export interface TenantAccessRoleProps {
  /** The custom connector's registration, whose `RuntimeRoleArn` and `ExternalId` the trust names. */
  readonly registration: CustomResource;
  readonly serviceAccount: string;
  readonly roleName: string;
  readonly description: string;
  readonly policy: TenantAccessPolicy;
  readonly boundary: IManagedPolicy;
}

/**
 * A role in the tenant account that trusts only one custom connector's runtime role, with its
 * external ID. The trust names the service account and pins the runtime role with
 * `aws:PrincipalArn`: IAM rejects a role principal it can't resolve yet, and the API creates the
 * runtime role seconds before this role, so naming the role itself fails while IAM propagates it.
 */
export class TenantAccessRole extends Construct {
  public readonly role: Role;

  constructor(scope: Construct, id: string, props: TenantAccessRoleProps) {
    const { registration, serviceAccount, roleName, description, policy, boundary } = props;
    super(scope, id);
    this.role = new Role(this, "Role", {
      roleName,
      description,
      assumedBy: new AccountPrincipal(serviceAccount).withConditions({
        StringEquals: {
          "sts:ExternalId": registration.getAttString(RegistrationAttribute.EXTERNAL_ID),
        },
        ArnEquals: {
          "aws:PrincipalArn": registration.getAttString(RegistrationAttribute.RUNTIME_ROLE_ARN),
        },
      }),
      inlinePolicies: { CcfConnectorAccess: policy.document },
      permissionsBoundary: boundary,
    });
    this.role.node.addDependency(registration);
    acknowledgeNagRules({
      scopes: [this.role],
      acknowledgments: [
        {
          id: "AwsSolutions-IAM5",
          reason: CONNECTOR_ROLE_WILDCARD_REASON,
          findings: resourceFindings({ scope: this, resources: policy.wildcardResources }),
        },
      ],
    });
  }
}
