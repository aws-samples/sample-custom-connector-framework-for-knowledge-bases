---
description: The example GitHub connector, an implementation of the Custom Connector Framework that indexes the public repositories of GitHub organizations.
---

# GitHub connector

The GitHub connector is an example implementation of the Custom Connector Framework. It reads the public repositories of the GitHub owners you choose, organizations or users, and keeps a destination current with their files. The framework doesn't depend on it. It shows what a complete connector looks like, and a new connector can copy it as a template.

It's also useful on its own. An index of your organizations' repositories lets people and agents search for code that already exists before they write it again. The [GitHub tutorial](../getting-started/tutorial.md) deploys it, and the blog posts show it with [AWS Quick](../blog/aws-quick.md) and [Strands Agents](../blog/strands-agents.md).

## What a job does

The connector's CDK app registers one custom connector per owner, named `github-<owner>`. Each job:

1. Lists the owner's repositories, most recently pushed first. It stops at the first repository with no push in `max_repository_age_days`, or at `max_repositories`, and skips names that match `excluded_repository_patterns`. The defaults leave out the AWS SDKs and the repositories generated from the same service models, which are documented elsewhere.
1. Treats each repository as a collection. When a repository's last push and the content filters match its checkpoint, the job skips it without downloading it.
1. Otherwise, streams the repository's default-branch archive straight from GitHub, without writing it to disk, and writes each kept file as a document. It deletes the documents of files that are gone, and saves the repository's checkpoint.
1. Deletes the documents of repositories that dropped out of the selection.

Every `full_read_interval_days`, the job reads every repository again, even if its last push hasn't changed.

The connector paces its GitHub requests, waits out GitHub's rate limits, and retries server errors and broken downloads. A job that stops partway keeps the repositories it finished, and the next job skips them.

## The documents it writes

Each document is one file of a repository, with the file's GitHub URL as its source URI and these attributes:

| Attribute | Value |
| --- | --- |
| `owner` | The repository's owner, such as `aws-samples` |
| `repository` | The repository's full name, `<owner>/<name>` |
| `repository_id` | `repo_` plus a hash of GitHub's numeric repository ID, which survives renames |
| `path` | The file's path in the repository |
| `repo_created_at` | The repository's creation time, in epoch seconds |

You can filter an [Amazon Bedrock](https://aws.amazon.com/bedrock/) `Retrieve` call on any of them, for example to search only one organization, as [Search your documents](../developer-guide/search.md#search-with-amazon-bedrock) shows.

## Where the code is

| Part | What it is |
| --- | --- |
| [`connectors/github/backend`](https://github.com/aws-samples/sample-custom-connector-framework-for-knowledge-bases/tree/main/connectors/github/backend) | `GitHubConnectorImplementation`, and the Dockerfile of its image |
| [`connectors/github/cdk`](https://github.com/aws-samples/sample-custom-connector-framework-for-knowledge-bases/tree/main/connectors/github/cdk) | The `GitHubCustomConnector` stack: the image and its `ccf-connectors/github` repository, the token secret, and one custom connector per owner |

[Configuration](configuration.md) describes every setting, and the [operations runbook](operations.md) covers its failures.
