# API model

The Smithy model in [`model/`](./model) defines the Custom Connector Framework
control-plane API. It is the maintained source for operations, shapes, HTTP
bindings, errors, and constraints.

`make install` syncs the Python environment and downloads the pinned Smithy CLI
into `.smithy-cli/`, verifying the release zip's SHA-256 checksum before using
it. `make build` runs the generation chain:

1. `smithy ast` writes `generated/model.json` for contract checks.
2. `smithy build` writes the Smithy OpenAPI artifact. The build copies it to
   `generated/openapi.json` and adds the [Amazon API
   Gateway](https://aws.amazon.com/api-gateway/) request validator, [AWS
   Signature Version 4 (SigV4)](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_sigv.html)
   security definition, [AWS Lambda](https://aws.amazon.com/lambda/)
   integration placeholder, and code-generation metadata.
3. `datamodel-codegen` generates the strict Pydantic v2 request, response, and
   enum types in `packages/ccf-api-models/` from that OpenAPI document.
4. `codegen/generate_client.py` reads the same OpenAPI document and generates
   the private typed operation methods and HTTP bindings in
   `toolkit/src/custom_connector_framework/_generated_client.py`.

The [AWS Cloud Development Kit (AWS CDK)](https://aws.amazon.com/cdk/) imports
`generated/openapi.json` into `SpecRestApi` and replaces the one
Lambda integration placeholder with the deployed backend function. The backend
parses requests and renders responses with the generated Pydantic models. The
public toolkit `CcfClient` inherits the generated operation methods and keeps
endpoint validation, SigV4 signing, transport, and modeled error handling in its
handwritten private core. No second operation or route inventory is maintained.

`make check` regenerates the contract, validates Smithy, lints and type-checks
the code-generation scripts, compares Smithy operations with OpenAPI, and checks
that the generated client is current. `make test` imports and exercises the
generated Pydantic package.

Change Smithy first, then run `make build`. Do not edit `generated/openapi.json`,
`packages/ccf-api-models/src/ccf_api_models/models.py`, or
`toolkit/src/custom_connector_framework/_generated_client.py`; the build replaces
them.
