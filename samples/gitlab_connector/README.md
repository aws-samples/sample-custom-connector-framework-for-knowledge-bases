# GitLab connector

The GitLab sample yields selected repository files, issues, and merge requests from every configured project as one complete snapshot. It does not fetch comments, discussions, diffs, reviews, jobs, or pipelines.

## Connector configuration

```json
{
  "token_secret_arn": "arn:aws:secretsmanager:us-east-1:111122223333:secret:gitlab-token",
  "server_url": "https://gitlab.com",
  "projects": [
    { "project_id": "group/project-one", "ref": "main" },
    {
      "project_id": "group/project-two",
      "ref": "release",
      "file_extensions": [".md", ".py"]
    }
  ]
}
```

The source role needs `secretsmanager:GetSecretValue` on the exact token secret. The connector uses the source-account session from `ConnectorContext` only to read that secret. It uses the toolkit's `EmptyJobConfiguration`, so job configuration is `{}`.

Configured `project_id` values select projects and remain file metadata values. File identity retains the existing hash of provider, immutable `project.id`, and complete source path. Issue and merge-request identity hashes provider, immutable `project.id`, category (`issue` or `merge_request`), and project-scoped IID. Project renames do not change IDs, equal paths or IIDs in different projects do not collide, and an issue cannot collide with a merge request that has the same IID.

Repository enumeration uses python-gitlab iterator mode with a 100-item page size. Issues use `project.issues.list(state="all", iterator=True, per_page=100)` and merge requests use `project.mergerequests.list(state="all", iterator=True, per_page=100)`. Issue and merge-request descriptions are deterministic UTF-8 Markdown with this field order: title, IID, state, author, created timestamp, updated timestamp, and description. The filename is `issue-<iid>.md` or `merge-request-<iid>.md`. Metadata contains the title, source URL, created and updated timestamps, provider, category, and state. Missing description or author values become empty text.

Removing a project from the configuration deletes all of its documents on the next run. If reading any file or category fails, the run stops before anything is written or deleted.

## Runtime

CCF runs this definition with:

```text
ccf-connector run gitlab_connector.connector:definition
```
