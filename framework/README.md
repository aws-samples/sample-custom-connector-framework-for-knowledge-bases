# Framework

This folder holds the Custom Connector Framework (CCF): the service that manages
custom connectors and runs their jobs, and the libraries connector projects build on. The service
deploys in the service account. The libraries, the Python toolkit and the CDK
constructs, go into each destination and connector project, such as the
[GitHub connector](../connectors/github).

| Subproject | What it is |
| --- | --- |
| [`api/`](./api) | The Smithy model of the API, the OpenAPI document generated from it, and the generated Pydantic models every other subproject uses |
| [`backend/`](./backend) | The Lambda handlers behind the API, the scheduled start, and the ingestion consumer |
| [`toolkit/`](./toolkit) | The `custom_connector_framework` Python package: `CcfClient`, the job runner, `DocumentStore`, and the `testing` module |
| [`cdk/`](./cdk) | The `CustomConnectorFramework`, `CustomConnectorFrameworkOps`, and `CustomConnectorDestinationFor<Name>` stacks, and the constructs destination and connector projects use, such as `CustomConnectorProject` |

## Build and test

Each make target (`install`, `build`, `check`, `test`, `fix`, `clean`) runs the same target in `api`, `toolkit`, `backend`, and `cdk`, in that order. The API goes first because the others use its generated models. `make check` checks that the API's generated files are current, then runs lint, type checks, and unit tests.

## Deploy

The framework deploys from the repository root:

```bash
make -C framework deploy CONFIG=config/default.json
```

`CONFIG` is relative to `cdk/`. The deploy runs `CustomConnectorFramework`, then `CustomConnectorFrameworkOps`.

## Documentation

The [documentation site](https://aws-samples.github.io/sample-custom-connector-framework-for-knowledge-bases/) covers the framework from each reader's side. The [Administrator Guide](https://aws-samples.github.io/sample-custom-connector-framework-for-knowledge-bases/administrator-guide/) covers configuring, deploying, securing, and monitoring it. The [Developer Guide](https://aws-samples.github.io/sample-custom-connector-framework-for-knowledge-bases/developer-guide/) covers building connectors on it, and [Architecture](https://aws-samples.github.io/sample-custom-connector-framework-for-knowledge-bases/architecture/) covers its components, workflows, and decisions.
