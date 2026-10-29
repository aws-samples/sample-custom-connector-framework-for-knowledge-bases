# GitHub connector

The GitHub sample yields selected repository files, issues, and pull requests from every configured repository as one complete snapshot. It does not fetch comments, reviews, diffs, workflow runs, or pipelines.

## Connector configuration

```json
{
  "token_secret_arn": "arn:aws:secretsmanager:us-east-1:111122223333:secret:github-token",
  "api_url": "https://api.github.com",
  "repositories": [
    { "full_name": "organization/repository-one", "ref": "main" },
    {
      "full_name": "organization/repository-two",
      "ref": "release",
      "file_extensions": [".md", ".py"]
    }
  ]
}
```

The source role needs `secretsmanager:GetSecretValue` on the exact token secret. The connector uses the source-account session from `ConnectorContext` only to read that secret. It uses the toolkit's `EmptyJobConfiguration`, so job configuration is `{}`.

`full_name` is a lookup value and file metadata value. File identity retains the existing hash of provider, immutable `repository.id`, and complete source path. Issue and pull-request identity hashes provider, immutable `repository.id`, category (`issue` or `pull_request`), and repository-scoped number. Repository renames do not change IDs, equal paths or numbers in different repositories do not collide, and an issue cannot collide with a pull request that has the same number.

The generator walks each Git tree without the recursive-tree truncation limit and rejects an incomplete tree. It calls `get_issues(state="all")`, excludes entries whose `pull_request` field is present, and calls `get_pulls(state="all")` for pull requests. Issue and pull-request bodies are deterministic UTF-8 Markdown with this field order: title, number, state, author, created timestamp, updated timestamp, and body. The filename is `issue-<number>.md` or `pull-request-<number>.md`. Metadata contains the title, source URL, created and updated timestamps, provider, category, and state. Missing body or author values become empty text.

Removing a repository from the configuration deletes all of its documents on the next run. If reading any file or category fails, the run stops before anything is written or deleted.

## Runtime

CCF runs this definition with:

```text
ccf-connector run github_connector.connector:definition
```
