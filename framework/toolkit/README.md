# Toolkit

The toolkit is the `custom_connector_framework` Python package. A connector
implementation imports it, and every connector image runs its
`ccf-connector run <module>:<class>` command. It holds:

| Module | What it is |
| --- | --- |
| `implementation.py`, `job_context.py` | `ConnectorImplementation`, the class an implementation subclasses, and `JobContext`, what it knows about its job |
| `store/` | `DocumentStore`: writes files and metadata files to the destination bucket and records them, in each destination's metadata format |
| `runner/`, `launcher.py` | The job runner: reads the job's inputs, assumes the source-access role, sweeps unrecorded files, runs the implementation, and exits with a code the framework maps to `status_reason` |
| `client/` | `CcfClient`, the typed API client, which reads each operation's binding from its packaged copy of `openapi.json` |
| `log_lines.py` | JSON log lines on stdout |
| `testing/` | `run_implementation`, which runs an implementation against in-memory fakes with no AWS account |

The [user guide](../../docs/user-guide.md#write-a-connector-implementation)
documents the implementation interface, the store, checkpoints, logging, and
the testing module, with a runnable example that
`tests/docs/test_user_guide_example.py` runs.

## Build and test

Run these from `framework/toolkit`:

```bash
make install   # uv sync, with the API's generated models
make check     # ruff, mypy, and the tests in tests/
```

`check`, `build`, and `test` first copy
`../api/generated/openapi.json` into `client/openapi.json` when it changed.
