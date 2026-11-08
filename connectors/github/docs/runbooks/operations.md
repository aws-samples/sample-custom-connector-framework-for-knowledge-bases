# GitHub connector runbook

This runbook is for the operator of the GitHub custom connectors. There is one
per GitHub owner, an organization or a user, named `github-<owner>`. The GitHub
app adds no alarm or dashboard of its own. A failed job raises the framework's
`JobsFailed` alarm in the service account, which links to the
[framework runbook](../../../../framework/docs/runbooks/delivery-failures.md#failed-jobs).
This page covers what is specific to GitHub.

Never paste the GitHub token, an external ID, or document content into a
ticket or a chat.

## Read a job's log events

The connector publishes no metrics, so triage starts from the JSON log events
in the custom connector's log group, `/ccf/connectors/github-<owner>`, in the
tenant account. Each job writes one stream, named after the job ID. The log
group exists only when `logDelivery` is `true` in the configuration; without
it, nothing the job prints is kept.

Each line is one JSON object whose `message` is the event name, with the
`job_id` and `connector_id` of the job. The connector writes these events:

| Event | Fields | Meaning |
| --- | --- | --- |
| `repositories_selected` | `owner`, `repositories` | How many repositories the job selected |
| `repository_excluded` | `repository` | The name matches `excluded_repository_patterns`, so the job left it out |
| `repository_skipped` | `repository`, `reason` `unchanged` | Its last push and the content filters match its checkpoint, so the job didn't download it |
| `repository_read` | `repository`, `documents`, `deleted` | The job read the archive, wrote the kept files, and deleted the files that are gone |
| `archive_download_retried` | `repository`, `attempt` | The archive download broke off, and the job reads the archive again |
| `files_skipped` | `excluded_path`, `too_large` | Files of the last archive left out: paths matching `excluded_path_patterns`, and files over `max_file_bytes` |
| `repository_dropped` | `repository`, `reason` `not_found` | GitHub no longer has the repository or its archive |
| `repository_collection_deleted` | `collection_id` | A repository left the selection, so its documents were deleted |
| `owner_read_completed` | `owner`, `full_read`, `repositories_read`, `repositories_unchanged`, `collections_deleted` | The job finished reading the owner |

The toolkit adds its own events to the same stream: `document_skipped` (with
`reason` `empty_content` or `unsupported_content`), `document_delete_queued`,
`collection_deleted`, `sweep_finished`, `job_terminated`, `job_start_failed`,
`runtime_settings_invalid`, and `connector_implementation_failed`, which holds
the exception's `stack_trace`.

To read one event type of a job, filter its stream:

```bash
aws logs filter-log-events --log-group-name /ccf/connectors/github-<owner> \
  --log-stream-names "<job ID>" --filter-pattern '{ $.message = "owner_read_completed" }'
```

## Read a failed job

Every GitHub failure starts with the job's own record:

1. Read the job's `status_reason` with `GetCustomConnectorJob`. The
   [administrator guide](../../../../docs/administrator-guide.md#troubleshoot-a-failed-job)
   maps each reason to its cause.
1. For `ConnectorImplementationError` or `JobTerminated`, read the stream the
   reason names. The `connector_implementation_failed` event holds the
   exception and its stack trace.
1. Match the failure to a section below, fix it, and start a job.

A job that stays `STARTED` or `RUNNING` after its execution ended keeps its
custom connector `IN_USE`. The
[user guide](../../../../docs/user-guide.md#jobs) describes how a new start or
`StopCustomConnectorJob` ends it.

## Missing, expired, or rejected token

CloudFormation creates the token secret with a generated placeholder. Until
you store a real token, and after it expires, GitHub answers with
`401 Bad credentials` and the job fails with `ConnectorImplementationError`.
The job fails with `SecretUnavailable` when the secret can't be read at all.

1. Store a working token in the secret named by the `GitHubTokenSecretArn`
   output of `GitHubCustomConnector`:

   ```bash
   read -rs GITHUB_TOKEN
   aws secretsmanager put-secret-value --secret-id "<GitHubTokenSecretArn>" \
     --secret-string "$GITHUB_TOKEN"
   unset GITHUB_TOKEN
   ```

1. Start a job and confirm it succeeds.

## GitHub rate limits or errors

The `github` block of `connectorConfiguration` sets how the connector paces
and retries its requests. It waits `request_interval_seconds` between API
requests. It retries a server error or a rate-limited request
`retry_attempts` times, with `retry_backoff_seconds` as the backoff factor.
It waits up to `max_rate_limit_wait_seconds` for a rate limit to reset. A
broken archive download is read again from the start, up to `retry_attempts`
times, and each retry logs `archive_download_retried`. A job fails when GitHub
keeps failing past those limits.

1. If the failures line up with a GitHub incident, start a job after it ends.
1. If jobs keep running out of rate limit, raise
   `github.request_interval_seconds` or lower `max_repositories`, and redeploy
   `GitHubCustomConnector`.

## Job runs out of time or memory

A job that hits its timeout fails with a `status_reason` that starts
`JobTerminated: Job attempt duration exceeded timeout`. A job killed for memory fails with Batch's reason, for
example `OutOfMemoryError: Container killed due to memory usage`.

1. Raise `connectorCompute.timeoutHours` or `connectorCompute.memoryMib`, or
   set `connectorCompute` for that owner under `owners.<owner>`. Lowering
   `max_repositories` also shortens a job.
1. Redeploy `GitHubCustomConnector` and start a job.

Repositories a failed job finished keep their new documents and checkpoints,
so the next job skips them unless they changed again.

## Files or repositories are missing from search

The connector or the toolkit can leave a file out, or the knowledge base can
miss an ingestion:

1. Read the `files_skipped` events of the job. `excluded_path` counts files
   that match `excluded_path_patterns`, and `too_large` counts files over
   `max_file_bytes`. A `document_skipped` event names a file the toolkit left
   out because it was empty or of a type the destination can't index.
1. Check that the repository was selected. For an owner with an `ORG` source,
   the job lists the owner's repositories most recently pushed first. It stops
   at the first repository with no push in `max_repository_age_days`, or once
   it has selected `max_repositories`. A name that matches
   `excluded_repository_patterns` is logged as `repository_excluded` and
   doesn't count toward `max_repositories`. A repository that leaves the
   selection is logged as `repository_collection_deleted`. `REPOSITORY`
   sources are read by name, and neither the age limit nor the name patterns
   apply to them.
1. Check that the destination's last ingestion finished, as the
   [framework runbook](../../../../framework/docs/runbooks/delivery-failures.md#ingestion-failures)
   describes.

## Start a job by hand

Call `StartCustomConnectorJob` with the destination ID and the custom
connector ID. The destination ID is the `DestinationId` output of the
`CustomConnectorDestinationFor<Name>` stack, which the configuration's
`destination.id` also holds. The custom connector IDs are the outputs of
`GitHubCustomConnector` whose keys start with `ConnectorId`. Each key ends
with the owner's name without its hyphens, for example `ConnectorIdawssamples`
for `aws-samples`. The [README](../../../../README.md#step-4-run-a-job) has a
script that starts a job and waits for it.
