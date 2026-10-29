import { BundlingOptions, RemovalPolicy } from "aws-cdk-lib";
import { Runtime } from "aws-cdk-lib/aws-lambda";

// The backend Lambdas run on x86_64, so bundling installs x86_64 wheels and must
// import-check them in an x86_64 container even on an arm64 build host.
const LAMBDA_BUNDLE_PLATFORM = "linux/amd64";

// Importing a handler runs its composition root, which creates boto3 clients and
// validates required configuration. These placeholders let the bundle's import
// check run offline; the deployed functions receive real values from the stack.
const IMPORT_CHECK_ENVIRONMENT = {
  AWS_DEFAULT_REGION: "us-east-1",
  AWS_EC2_METADATA_DISABLED: "true",
  CONFIGURATION_KEY_ARN:
    "arn:aws:kms:us-east-1:000000000000:key/00000000-0000-0000-0000-000000000000",
  INGESTION_BUCKET_NAME: "ccf-import-check",
  RUNTIME_ROLE_BOUNDARY_ARN: "arn:aws:iam::000000000000:policy/CcfConnectorRuntimeBoundary",
  CONNECTOR_RUNTIME_ROUTES: '[{"method":"GET","path":"/api/v1/custom-connectors/{connector_id}"}]',
  ALLOWED_CLIENT_ACCOUNT_IDS: "000000000001",
};

/**
 * Shared bundling configuration for the backend Lambda source.
 *
 * Every backend handler is packaged from the same `backend/src` tree the same
 * way: install the pinned `requirements.txt` for the Lambda runtime platform,
 * then copy the source alongside it. One definition here keeps the handlers from
 * drifting apart.
 */
export interface DockerImageProps {
  /** Absolute path to the repository root containing backend/ and api/. */
  readonly sourcePath: string;
  /** The bundling options applied to that source. */
  readonly bundling: BundlingOptions;
  /** Removal policy for each handler's log group. */
  readonly logRemovalPolicy: RemovalPolicy;
}

/**
 * Builds the shared {@link DockerImageProps} from the repository root at
 * `sourcePath`. The output contains only backend runtime files plus the generated
 * model package; generated source remains owned by api/.
 */
export const createBackendImage = (
  sourcePath: string,
  logRemovalPolicy: RemovalPolicy,
): DockerImageProps => ({
  sourcePath,
  logRemovalPolicy,
  bundling: {
    image: Runtime.PYTHON_3_14.bundlingImage,
    platform: LAMBDA_BUNDLE_PLATFORM,
    command: [
      "bash",
      "-c",
      [
        "pip install --no-cache-dir -r backend/src/requirements.txt",
        "--platform manylinux2014_x86_64 --target /asset-output --only-binary=:all:",
        "&& cp -au backend/src/. /asset-output",
        "&& cp -au api/packages/ccf-api-models/src/ccf_api_models /asset-output/ccf_api_models",
        "&& python -c 'import sys; sys.path.insert(0, \"/asset-output\"); import api_handler, ccf_api_models'",
      ].join(" "),
    ],
    environment: { PIP_NO_CACHE_DIR: "true", ...IMPORT_CHECK_ENVIRONMENT },
  },
});
