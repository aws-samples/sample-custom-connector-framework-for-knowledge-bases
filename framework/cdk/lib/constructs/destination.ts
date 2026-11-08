import { CustomResource, Duration, RemovalPolicy, Stack, Token } from "aws-cdk-lib";
import { RetentionDays } from "aws-cdk-lib/aws-logs";
import { Construct } from "constructs";
import {
  enumMember,
  enumValues,
  maxItems,
  operationRoutes,
  stringPattern,
} from "../common/contracts";
import { destinationBucketName } from "../common/destination-names";
import {
  ConnectorOpsModule,
  ConnectorOpsProvider,
  frameworkApiEnvironment,
} from "./connector-ops-provider";
import { DestinationRole } from "./destination-role";
import { FrameworkValues } from "./framework-values";

/** The custom resource's attributes, which `destination.py` returns. */
export enum DestinationAttribute {
  DESTINATION_ID = "DestinationId",
  BUCKET_NAME = "BucketName",
  KNOWLEDGE_BASE_ID = "KnowledgeBaseId",
  DATA_SOURCE_ID = "DataSourceId",
}

/** One destination, created in this stack or imported; the knowledge base IDs are set only for `BEDROCK`. */
export interface IDestination {
  readonly destinationId: string;
  readonly bucketName: string;
  readonly knowledgeBaseId: string | undefined;
  readonly dataSourceId: string | undefined;
}

/** A destination another stack created, from its outputs. */
export interface DestinationAttributes {
  readonly destinationId: string;
  readonly bucketName: string;
  readonly knowledgeBaseId?: string;
  readonly dataSourceId?: string;
}

/** A destination another stack created, by ID; its bucket name follows from the ID, account, and service Region. */
export interface DestinationIdAttributes {
  readonly destinationId: string;
  readonly framework: FrameworkValues;
}

/** One custom metadata field of a `BEDROCK` destination's knowledge base, as the API's `MetadataField` holds it. */
export interface MetadataFieldProps {
  readonly name: string;
  /** One of the API's `MetadataFieldType` values. */
  readonly type: string;
  /** One of the API's `MetadataFieldSearchability` values. */
  readonly searchability: string;
}

export interface DestinationProps {
  readonly framework: FrameworkValues;
  /** The destination's own role; the destination is deleted before the role and its boundaries. */
  readonly role: DestinationRole;
  readonly name: string;
  /** One of the API's `DestinationType` values. */
  readonly type: string;
  /** Whether the `BEDROCK` data source enforces access control lists. Fixed at creation. */
  readonly accessControlEnabled: boolean;
  /**
   * The knowledge base's custom metadata fields, `BEDROCK` only. A change calls `UpdateDestination` and keeps the
   * destination; a field's type can't change, so remove it in one deployment and add it back in the next.
   */
  readonly metadataFields?: readonly MetadataFieldProps[];
  /** For the provider's log groups. */
  readonly logRetention: RetentionDays;
  /** `RETAIN` keeps the destination and its documents when the resource leaves the stack. */
  readonly removalPolicy: RemovalPolicy;
  /** How long the provider waits for the destination to become `ACTIVE`, or to be gone. */
  readonly totalTimeout: Duration;
}

const validateDestinationId = (destinationId: string): void => {
  if (!Token.isUnresolved(destinationId) && !stringPattern("DestinationId").test(destinationId))
    throw new Error(`destinationId "${destinationId}" must be "dst-" and 12 hex digits.`);
};

interface MetadataFieldsCheck {
  readonly type: string;
  readonly fields: readonly MetadataFieldProps[];
}

/** The API's `MetadataFieldList` rules, at synthesis instead of in the deployment. */
const validateMetadataFields = ({ type, fields }: MetadataFieldsCheck): void => {
  if (fields.length > 0 && type !== enumMember({ schema: "DestinationType", member: "BEDROCK" }))
    throw new Error("metadataFields may be set only for a BEDROCK destination.");
  const limit = maxItems({ schema: "Destination", member: "metadata_fields" });
  if (fields.length > limit) throw new Error(`metadataFields holds at most ${limit} fields.`);
  const names = fields.map((field) => field.name);
  if (new Set(names).size !== names.length)
    throw new Error("metadataFields must name each field once.");
  for (const field of fields) {
    if (!stringPattern("MetadataFieldName").test(field.name))
      throw new Error(
        `metadata field name "${field.name}" must be 1 to 200 letters, digits, '_', or '-', not starting with '_' or '-'.`,
      );
    for (const [member, schema] of [
      [field.type, "MetadataFieldType"],
      [field.searchability, "MetadataFieldSearchability"],
    ] as const)
      if (!enumValues(schema).includes(member))
        throw new Error(
          `metadata field "${field.name}": "${member}" must be one of ${enumValues(schema).join(", ")}.`,
        );
  }
};

/**
 * One destination, registered with `CreateDestination` and `ACTIVE` before the stack finishes:
 * the service's destination workflow builds its bucket and, for `BEDROCK`, its knowledge base,
 * data source, and knowledge base role through the destination's own role. `FAILED` fails the
 * deployment with the destination's `status_reason`. Once the destination is `ACTIVE`, the provider
 * sets `metadataFields` with `UpdateDestination`, so a change to them alone keeps the destination;
 * any other change replaces it. Deleting the resource, including
 * on rollback, calls `DeleteDestination` and waits until the destination is gone.
 */
export class Destination extends Construct implements IDestination {
  public readonly resource: CustomResource;
  public readonly destinationId: string;
  public readonly bucketName: string;
  public readonly knowledgeBaseId: string | undefined;
  public readonly dataSourceId: string | undefined;

  /** A destination another stack created, from its outputs; creates nothing. */
  public static fromDestinationAttributes(
    _scope: Construct,
    _id: string,
    attributes: DestinationAttributes,
  ): IDestination {
    const { destinationId, bucketName, knowledgeBaseId, dataSourceId } = attributes;
    validateDestinationId(destinationId);
    return { destinationId, bucketName, knowledgeBaseId, dataSourceId };
  }

  /** A destination another stack created, by ID; its knowledge base IDs are unknown. */
  public static fromDestinationId(
    scope: Construct,
    id: string,
    attributes: DestinationIdAttributes,
  ): IDestination {
    const { destinationId, framework } = attributes;
    const account = Stack.of(scope).account;
    const region = framework.frameworkApi().region;
    return Destination.fromDestinationAttributes(scope, id, {
      destinationId,
      bucketName: destinationBucketName({ destinationId, account, region }),
    });
  }

  constructor(scope: Construct, id: string, props: DestinationProps) {
    super(scope, id);
    const { framework, role, name, type, accessControlEnabled, removalPolicy } = props;
    const metadataFields = props.metadataFields ?? [];
    if (!enumValues("DestinationType").includes(type))
      throw new Error(`type "${type}" must be one of ${enumValues("DestinationType").join(", ")}.`);
    validateMetadataFields({ type, fields: metadataFields });
    const api = framework.frameworkApi();
    const provider = ConnectorOpsProvider.of({
      scope: this,
      props: {
        module: ConnectorOpsModule.DESTINATION,
        environment: frameworkApiEnvironment(api),
        logRetention: props.logRetention,
        totalTimeout: props.totalTimeout,
      },
    });
    provider.allowRoutes({
      routes: operationRoutes([
        "CreateDestination",
        "GetDestination",
        "UpdateDestination",
        "DeleteDestination",
      ]),
      api,
    });
    this.resource = new CustomResource(this, "Destination", {
      serviceToken: provider.serviceToken,
      resourceType: "Custom::CcfDestination",
      removalPolicy,
      properties: {
        Name: name,
        Type: type,
        AccessControlEnabled: accessControlEnabled,
        RoleArn: role.roleArn,
        MetadataFields: metadataFields.map((field) => ({
          Name: field.name,
          Type: field.type,
          Searchability: field.searchability,
        })),
      },
    });
    // The workflow attaches the role's boundary to the knowledge base role, so the destination,
    // which deletes that role, goes first; then the destination role, then its boundaries.
    this.resource.node.addDependency(role);
    this.destinationId = this.resource.getAttString(DestinationAttribute.DESTINATION_ID);
    this.bucketName = this.resource.getAttString(DestinationAttribute.BUCKET_NAME);
    const bedrock = type === enumMember({ schema: "DestinationType", member: "BEDROCK" });
    this.knowledgeBaseId = bedrock
      ? this.resource.getAttString(DestinationAttribute.KNOWLEDGE_BASE_ID)
      : undefined;
    this.dataSourceId = bedrock
      ? this.resource.getAttString(DestinationAttribute.DATA_SOURCE_ID)
      : undefined;
  }
}
