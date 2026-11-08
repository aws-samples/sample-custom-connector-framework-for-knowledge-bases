# GitHub connector

The GitHub connector is an example implementation of the Custom Connector Framework. It reads the most recently pushed public repositories of the GitHub owners you choose into a destination, with one custom connector per owner. The default configuration reads the `aws-samples`, `awslabs`, and `aws` organizations. It's also the template a new connector project copies: one implementation class, and a CDK app with one connector stack.

The [GitHub connector documentation](https://aws-samples.github.io/sample-custom-connector-framework-for-knowledge-bases/github-connector/) describes what a job does, the documents it writes, every configuration setting, and its operations runbook.

| Part | What it is |
| --- | --- |
| [`backend/`](./backend) | `GitHubConnectorImplementation` in `src/github_connector.py`, and the Dockerfile of its image |
| [`cdk/`](./cdk) | The `GitHubCustomConnector` stack: the connector image and its `ccf-connectors/github` repository, the token secret, and one custom connector per owner, in an existing destination |

## Build and test

Run these from the repository root:

```bash
make -C connectors/github install
make -C connectors/github check
```

In `backend`, `check` runs ruff, mypy, and the tests. In `cdk`, it runs
tsc, Prettier, eslint, and Jest. `GITHUB_TOKEN=<token> uv run pytest tests/integ` in `backend/`
reads live GitHub through the real connector and the toolkit's in-memory fakes.
Without a token, those tests are skipped.

`make -C connectors/github/backend build` writes
`backend/generated/connector-configuration.schema.json`, the JSON Schema of
`GitHubConnectorConfiguration`. The CDK app checks each custom connector's
configuration against that file, so run `build` after you change the model.
The backend's `check` fails while the file is out of date.

## Deploy

The connector deploys after its destination. Deploy the destination's stack first,
which creates the destination through the framework API from `cdk/config/destination.json`
(`make deploy-destination`). Set the destination's ID as `destination.id` in the
configuration, then deploy:

```bash
make -C connectors/github deploy CONFIG=config/default.json
```

`CONFIG` is relative to `cdk/`. Then store a GitHub token in the secret named by the `GitHubTokenSecretArn` output. The [tutorial](https://aws-samples.github.io/sample-custom-connector-framework-for-knowledge-bases/getting-started/tutorial/) walks through it.
