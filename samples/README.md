# Sample connectors

Each sample is a complete connector definition built on the toolkit's
[author contract](../toolkit/README.md#author-contract). None of the samples
needs per-job settings, so each uses the toolkit's `EmptyJobConfiguration`, and
none reimplements reconciliation: each definition yields a full snapshot and the
toolkit turns it into creates, updates, and deletes. Each connector's README
covers what differs per source, which is its configuration, how it builds
document IDs, and which source APIs it calls:

- [`github_connector`](./github_connector/README.md) reads files, issues, and
  pull requests from multiple GitHub repositories.
- [`gitlab_connector`](./gitlab_connector/README.md) reads files, issues, and
  merge requests from multiple GitLab projects.
- [`web_crawler`](./web_crawler/README.md) reads a configured set of HTTPS
  pages.

The GitHub and GitLab samples validate `api_url` and `server_url` with Pydantic
before retrieving a source credential. Both accept enterprise hostnames,
optional ports, and base paths, but require HTTPS with a hostname and reject
user information, query strings, and fragments.

## Local verification

```bash
make -C samples install
make -C samples check
uv run --directory samples pytest -p no:cacheprovider tests/test_reconciliation.py -q
make -C samples build
make -C samples smoke-clean
```

The local tests invoke the real `ConnectorLauncher` with `module:definition`,
fake source APIs, an in-memory S3 client, and a local fake CCF control plane. The
GitHub and GitLab scenarios exercise multiple repositories or projects through
initial creation, additions, content and metadata updates, per-category deletion,
whole-source removal, unchanged reruns, and managed sidecar rendering. Separate
partial-enumeration failures verify zero destination or state mutation. Other
scenarios cover destination-mode and renderer changes, crawler update and delete,
unsafe endpoints before credential access, private-address and redirect
rejection, response-size limits, and file-source failure during preflight.

Build the multi-stage, non-root image from the repository root, with `docker`
or `finch`:

```bash
finch build --pull -f samples/Dockerfile .
```

The image installs from `uv.lock` with `uv sync --frozen`. Its base images
track the latest `uv` release and the latest Python 3.14 slim image, so build
with `--pull` to pick up their current security patches. Its default command
runs the crawler definition; CCF supplies another definition command for the
GitHub or GitLab sample. These checks do not deploy AWS resources or verify a
managed ingestion. Use the
[non-production Bedrock smoke procedure](../docs/non-production-bedrock-smoke-test.md)
for that boundary.
