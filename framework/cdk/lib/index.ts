/**
 * The framework's CDK library for destination and connector projects, such as connectors/github:
 * the L2 constructs (`FrameworkValues`, `DestinationRole`, `Destination`, `ConnectorImage`,
 * `CustomConnector`), the L3 patterns (`CustomConnectorDestination`, `CustomConnectorProject`),
 * and the destination stack. The framework's own app, `app.ts`, imports its modules directly.
 */
export {
  FRAMEWORK_ENDPOINT_PATTERN,
  FrameworkValues,
  frameworkLiteralsProblem,
} from "./constructs/framework-values";
export type { FrameworkValuesLiterals } from "./constructs/framework-values";
export { DestinationRole } from "./constructs/destination-role";
export type { DestinationRoleProps } from "./constructs/destination-role";
export { DestinationRoleName } from "./common/destination-names";
export { Destination } from "./constructs/destination";
export type {
  DestinationAttributes,
  DestinationIdAttributes,
  DestinationProps,
  IDestination,
} from "./constructs/destination";
export { ConnectorImage, repositoryNameProblem } from "./constructs/connector-image";
export type {
  ConnectorImageAttributes,
  ConnectorImageProps,
  IConnectorImage,
} from "./constructs/connector-image";
export {
  CustomConnector,
  computeProblem,
  sourceAccessServicesProblem,
} from "./constructs/custom-connector";
export type {
  CustomConnectorCompute,
  CustomConnectorProps,
  LogDeliveryProps,
} from "./constructs/custom-connector";
export { CustomConnectorDestination } from "./construct-groups/custom-connector-destination";
export type { CustomConnectorDestinationProps } from "./construct-groups/custom-connector-destination";
export { CustomConnectorProject } from "./construct-groups/custom-connector-project";
export type {
  AddCustomConnectorRequest,
  CustomConnectorProjectImageProps,
  CustomConnectorProjectProps,
} from "./construct-groups/custom-connector-project";
export {
  CustomConnectorDestinationStack,
  DestinationOutput,
  destinationStackName,
} from "./stacks/custom-connector-destination-stack";
export type { CustomConnectorDestinationStackProps } from "./stacks/custom-connector-destination-stack";
export { readAppEnvironment, readContextPath } from "./common/app-environment";
export type { AppEnvironment } from "./common/app-environment";
export {
  ConfigurationError,
  fieldName,
  isJsonObject,
  readBoolean,
  readChoice,
  readConfigFile,
  readInteger,
  readLogRetention,
  readNumber,
  readObject,
  readRemovalPolicy,
  readString,
  readStringList,
  requireValue,
} from "./common/app-config";
export type {
  IntegerBounds,
  JsonObject,
  ReadChoiceRequest,
  ReadConfigFileRequest,
  ReadFieldRequest,
  ReadIntegerRequest,
  ReadObjectRequest,
} from "./common/app-config";
export { destinationBucketPolicy } from "./common/destination-bucket-policy";
export { stringPattern } from "./common/contracts";
