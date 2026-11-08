---
title: "Inside the GitHub connector: a custom connector example"
description: How the example GitHub connector for the Custom Connector Framework indexes the public repositories of GitHub organizations into Amazon Bedrock knowledge bases and AWS Quick, and the design choices that keep it current at scale.
---

# Inside the GitHub connector: a custom connector example

A connector for a small source can read everything on every run. A connector for a GitHub organization can't. The `aws-samples` organization alone holds thousands of repositories, some of them gigabytes in size, and new pushes land every day. Reading all of it every week would take hours, use up the GitHub API rate limits, and make the knowledge base index the same files again and again.

In this post, we walk through the example GitHub connector that ships with the Custom Connector Framework. It's a complete connector you can deploy as it is, and it's also the template for your own. We focus on the choices it makes to stay current with large organizations, because the same choices apply to most sources.

![Banner titled "How the GitHub connector stays current", with the line "The choices that keep large organizations indexed without rereading everything." A hub labeled "GitHub connector on CCF" is surrounded by six design choices. They are one collection per repository, skip what didn't change, stream archives, leave out noise, respect rate limits, and filterable documents. Below the hub are four results: weekly jobs that finish, renames don't re-index, deleted repositories drop out, and a template for your own.](../assets/diagrams/blog-github-connector.png)

## What the Custom Connector Framework is

The Custom Connector Framework (CCF) is an open source AWS sample that simplifies building, running, and operating document ingestion from any document source into [Amazon Bedrock knowledge bases](https://aws.amazon.com/bedrock/knowledge-bases/), [AWS Quick](https://aws.amazon.com/quick/), or [Amazon S3](https://aws.amazon.com/s3/). You write the code that reads your source. CCF manages everything else: the API, the schedule, the compute, full and incremental sync, ingestion, the security boundary, and the alarms. It keeps each team's documents and credentials in that team's own AWS account.

That's where **faster time to production** comes from. The GitHub connector shows what **managed connector infrastructure** leaves for the connector to do: decide what to read, and read only what changed. Everything below is that code. None of it schedules jobs, writes files to Amazon S3, or starts ingestion, because CCF does.

## One collection per repository

CCF lets a connector group its documents into collections, each with its own checkpoint. The GitHub connector makes each repository a collection, and names it by the numeric GitHub repository ID instead of the repository name. A renamed repository keeps its collection, so a rename doesn't delete and re-index every file.

When a repository leaves the selection, because it was deleted, aged out, or excluded, the connector deletes its collection, and CCF deletes its documents. After the next ingestion, the knowledge base stops returning that repository's files.

## Skip what didn't change

A repository's last push time says whether anyone pushed to it. The connector saves it in the repository's checkpoint, together with a hash of the content filters, and compares both on the next job. When neither changed, it skips the repository without downloading it.

That one check keeps weekly jobs practical. A job over an organization lists every repository, but downloads only the ones pushed to since the last job. Within a downloaded repository, CCF's document store compares each file's content hash with the stored one, so an unchanged file isn't written again either.

The checkpoint holds only the push time and the filters, so it misses changes that neither one shows, such as a new default branch. So every `full_read_interval_days`, 28 by default, the connector reads every repository again.

## Read archives as a stream

To read a repository, the connector downloads the default branch's tarball and reads it straight from the HTTP response, file by file, without writing it to disk. A job needs no disk for the archive, however large the repository is.

The download asks for `refs/heads/<branch>` instead of the branch's short name. A tag can have the same name as the default branch. GitHub then can't tell which one a short name means, and answers with a list of choices instead of the archive. A full ref always names the branch.

## Leave out what isn't worth indexing

Not every repository or file helps a search. The default configuration leaves out:

- Repositories with no push in 730 days, and any after the 300 most recently pushed repositories the connector selects for an owner, set by `max_repository_age_days` and `max_repositories`.
- Repositories generated from AWS service models, such as the AWS SDKs, the Go bindings for the AWS CDK, and the API models. They're large, documented elsewhere, and would crowd out hand-written code. `aws-cdk-go` alone is about 23 GB.
- Build output, dependencies, binaries, and secrets, with `.gitignore`-style path patterns.
- Files larger than `max_file_bytes`, 100 MiB by default.

Each job logs how many files it left out and why, so you can tune the lists from real counts.

## Be a good API citizen

The connector waits between GitHub API requests, 2 seconds by default, and retries server errors and broken downloads. When it hits a rate limit, it waits for the limit to reset, up to 65 minutes by default, before the job fails. Each owner gets its own custom connector. The default configuration starts their weekly jobs 8 hours apart, to keep jobs for different organizations from competing for one token's rate limit.

When a job fails partway anyway, the repositories it finished keep their documents and checkpoints. The next job skips them and continues with the rest.

## Make every document filterable

Each document carries the attributes `owner`, `repository`, `repository_id`, `path`, and `repo_created_at`, and its GitHub URL as its source URI. A search can be narrowed to one organization or one repository, and every result links back to the file on GitHub.

## Try it, then make it yours

The [tutorial](../getting-started/tutorial.md) deploys the connector against the `aws-samples`, `awslabs`, and `aws` organizations. To index your own, change `sources` and `owners` in its [configuration](../github-connector/configuration.md). To build a connector for another source, copy its stack and its implementation. Keep the patterns that apply to your source: a collection per natural unit of the source, a checkpoint that says whether the unit changed, and a periodic full read.

[Build a custom connector for any document source](build-a-connector.md) starts from there.
