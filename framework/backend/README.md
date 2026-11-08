# Backend

The backend is the framework's Lambda code. It owns the API's state in
DynamoDB and starts the destination and connector deletion workflows. It
starts jobs, on a schedule or through the API, by starting the job's
`CcfJobExecution` execution, which runs the Batch job and records its result.
It also starts Bedrock ingestion. It never handles document bytes. Jobs write
files to the tenant's bucket, and the backend keeps only their records.

| Handler | Triggered by | Does |
| --- | --- | --- |
| `api_handler.py` | API Gateway | Serves the 21 API operations |
| `scheduled_start_handler.py` | EventBridge Scheduler | Starts a custom connector's scheduled job through the same path as `StartCustomConnectorJob` |
| `ingestion_consumer_handler.py` | The ingestion queue | Starts a destination's Bedrock ingestion through the destination's role |

Each handler is a composition root: it reads its typed settings, builds its
collaborators, and calls an activity in `src/activities/`. Data access lives in
`src/common/storage/`, validation in `src/common/validators/`, and request and
response models come from the API's generated `ccf_api_models` package.

## Settings

The CDK app sets every handler's environment. [`.env.example`](../../.env.example)
lists each setting with an example value, grouped by handler.

## Build and test

Run these from `framework/backend`:

```bash
make install   # uv sync, with the API's generated models
make check     # ruff, mypy, and the unit tests in tests/unit
make build     # imports every module, as the Lambda bundles do
```

The CDK app bundles `src/` into arm64 Lambda functions with the runtime
dependencies locked in `uv.lock`.

## More

[DESIGN.md](../../DESIGN.md) describes the workflows and decisions, and the
[user guide](../../docs/user-guide.md#call-the-api) describes the API.
