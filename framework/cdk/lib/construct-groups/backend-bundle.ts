import { BundlingOptions, DockerImage } from "aws-cdk-lib";
import { Architecture } from "aws-cdk-lib/aws-lambda";

/** Every framework Lambda runs on Graviton (arm64). */
export const LAMBDA_ARCHITECTURE = Architecture.ARM_64;

// Bundling runs in an arm64 container so installed wheels and the import check match the runtime.
export const LAMBDA_BUNDLE_PLATFORM = LAMBDA_ARCHITECTURE.dockerPlatform;

/**
 * The SAM build image for the Lambda Python 3.14 runtime, by version tag, so a bundle is
 * repeatable. It is multi-architecture; `LAMBDA_BUNDLE_PLATFORM` picks arm64. It ships the uv
 * that installs the dependencies locked in a uv.lock, so the tag pins uv too. Raise the tag to
 * pick up the runtime's patches.
 */
export const LAMBDA_BUNDLE_IMAGE = DockerImage.fromRegistry(
  "public.ecr.aws/sam/build-python3.14:1.171.0",
);

export const LAMBDA_BUNDLE_ENVIRONMENT = { UV_CACHE_DIR: "/tmp/uv-cache", UV_LINK_MODE: "copy" };

// The check imports the packages, not a handler module: a handler reads its settings at import.
const BACKEND_BUNDLE_COMMAND = [
  "uv export --quiet --project /asset-input/backend --frozen --no-dev --no-emit-project --no-emit-local --output-file /tmp/requirements.txt",
  "uv pip install --quiet --python python --target /asset-output --require-hashes --no-deps --no-cache -r /tmp/requirements.txt",
  "cp -au /asset-input/backend/src/. /asset-output",
  "cp -au /asset-input/api/packages/ccf-api-models/src/ccf_api_models /asset-output/ccf_api_models",
  "PYTHONPATH=/asset-output python -c 'import common, activities, ccf_api_models'",
].join(" && ");

/**
 * The one bundle of the backend Lambda source: the dependencies locked in `backend/uv.lock`, the
 * `backend/src` tree, and the API models. Every backend handler is packaged from it.
 */
export interface BackendBundle {
  /** Absolute path to the framework directory containing backend/ and api/. */
  readonly sourcePath: string;
  readonly bundling: BundlingOptions;
}

export interface BackendBundleRequest {
  readonly sourcePath: string;
}

export const createBackendBundle = ({ sourcePath }: BackendBundleRequest): BackendBundle => ({
  sourcePath,
  bundling: {
    image: LAMBDA_BUNDLE_IMAGE,
    platform: LAMBDA_BUNDLE_PLATFORM,
    command: ["bash", "-c", BACKEND_BUNDLE_COMMAND],
    environment: LAMBDA_BUNDLE_ENVIRONMENT,
  },
});
