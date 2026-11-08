# Framework

This folder is the Custom Connector Framework (CCF): the service that runs
custom connectors and the libraries connector projects build on. The service
deploys in the service account. The libraries, the Python toolkit and the CDK
constructs, go into each destination and connector project, such as the
[GitHub connector](../connectors/github).

| Subproject | What it is |
| --- | --- |
| [`api/`](./api) | The Smithy model of the API, the OpenAPI document generated from it, and the generated Pydantic models every other subproject uses |
| [`backend/`](./backend) | The Lambda handlers behind the API, the scheduled start, and the ingestion consumer |
| [`toolkit/`](./toolkit) | The `custom_connector_framework` Python package: `CcfClient`, the job runner, `DocumentStore`, and the `testing` module |
| [`cdk/`](./cdk) | The `CustomConnectorFramework`, `CustomConnectorFrameworkOps`, and `CustomConnectorDestinationFor<Name>` stacks, and the constructs destination and connector projects use, such as `CustomConnectorProject` |
| [`docs/runbooks/`](./docs/runbooks) | The runbook the framework's alarms link to |

## Build and test

Each make target (`install`, `build`, `check`, `test`, `fix`, `clean`) runs
the same target in `api`, `toolkit`, `backend`, and `cdk`, in that order. The
API goes first because the others use its generated models. `make check`
runs lint, type checks, and unit tests, and regenerates the API's generated
files first.

## Deploy

The framework deploys from the repository root:

```bash
make -C framework deploy CONFIG=config/default.json
```

`CONFIG` is relative to `cdk/`. The deploy runs `CustomConnectorFramework`,
then `CustomConnectorFrameworkOps`.

## More

The guides cover the framework from each reader's side:

- [README](../README.md): the end-to-end walkthrough.
- [Administrator guide](../docs/administrator-guide.md): configuration, tenants,
  roles, monitoring, and teardown.
- [User guide](../docs/user-guide.md): the API and writing a connector
  implementation.
- [DESIGN.md](../DESIGN.md): the glossary, components, workflows, and decisions.
