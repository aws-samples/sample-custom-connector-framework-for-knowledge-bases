# GitHub connector

The GitHub connector reads the most recently pushed public repositories of
GitHub owners into a destination, one custom connector per owner. The default
configuration reads the `aws-samples`, `awslabs`, and `aws` organizations. It is
also the template a new connector project copies: one implementation class, and
a CDK app with one connector stack.

| Part | What it is |
| --- | --- |
| [`backend/`](./backend) | `GitHubConnectorImplementation` in `src/github_connector.py`, and the Dockerfile of its image |
| [`cdk/`](./cdk) | The `GitHubCustomConnector` stack: the connector image and its `ccf-connectors/github` repository, the token secret, and one custom connector per owner, in an existing destination |
| [`docs/runbooks/operations.md`](./docs/runbooks/operations.md) | The runbook for GitHub failures |

## What a job does

Each job lists the owner's repositories, most recently pushed first, and stops
at the first one with no push in `max_repository_age_days` or at
`max_repositories`. It skips names matching `excluded_repository_patterns`. Each
repository is a collection. When its last push and the content filters match
its checkpoint, the job skips it. Otherwise it streams the repository's
archive, writes each kept file, and deletes the files that are gone.
Repositories that drop out are deleted. Every `full_read_interval_days`, the job
re-reads every repository.

Each document carries the attributes `owner`, `repository`, `repository_id`
(`repo_` plus a hash of GitHub's numeric repository ID, which survives renames),
`path`, and `repo_created_at` (epoch seconds), and the file's GitHub URL as its
source URI.

## Build and test

Run these from the repository root:

```bash
make -C connectors/github install
make -C connectors/github check
```

In `backend`, `check` runs ruff, mypy, and the unit tests. In `cdk`, it runs
tsc, eslint, and Jest. `GITHUB_TOKEN=<token> uv run pytest tests/integ` in `backend/`
reads live GitHub through the real connector and the toolkit's in-memory fakes,
and is skipped without a token.

`make -C connectors/github/backend build` writes
`backend/generated/connector-configuration.schema.json`, the JSON Schema of
`GitHubConnectorConfiguration`. The CDK app checks each custom connector's
configuration against that file, so run `build` after you change the model.
The backend's `check` fails while the file is out of date.

## Deploy

The connector deploys after its destination, which the framework's CDK app
creates (`make -C framework/cdk deploy DESTINATION_CONFIG=config/destination-github.json`).
Set the destination's ID as `destination.id` in the configuration, then deploy:

```bash
make -C connectors/github deploy CONFIG=config/default.json
```

`CONFIG` is relative to `cdk/`. Then store a GitHub token in the secret named by the
`GitHubTokenSecretArn` output. The [README](../../README.md#walkthrough-from-deploy-to-search)
walks through it, and the
[administrator guide](../../docs/administrator-guide.md#configure-the-github-connector)
describes every configuration key.
