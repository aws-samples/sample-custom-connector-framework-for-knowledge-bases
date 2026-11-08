---
description: The Custom Connector Framework (CCF) is an open source AWS sample that simplifies building, running, and operating document ingestion from any document source into Amazon Bedrock knowledge bases, AWS Quick, or Amazon S3.
---

# Custom Connector Framework

The Custom Connector Framework (CCF) is an open source AWS sample that simplifies building, running, and operating document ingestion from any document source into [Amazon Bedrock knowledge bases](https://aws.amazon.com/bedrock/knowledge-bases/), [AWS Quick](https://aws.amazon.com/quick/), or [Amazon S3](https://aws.amazon.com/s3/). You write the code that reads your source. CCF manages everything else a production connector needs, such as scheduling, sync, security, and operations.

![Code repositories, wikis, ticketing systems, knowledge articles, and file shares connect through the Custom Connector Framework to Amazon Bedrock knowledge bases, AWS Quick, and Amazon S3. Below the diagram, CCF's benefits are listed, starting with faster time to value.](assets/diagrams/hero.png)

Amazon Bedrock and AWS Quick ship connectors for common sources. Many teams also keep knowledge in systems that no built-in connector reads, such as an internal wiki, a ticketing tool, a document library, or hundreds of code repositories. CCF is how you bring those sources in without building a new ingestion pipeline for each one. It's built for text-based sources whose content benefits from vector search and retrieval-augmented generation (RAG).

## Why CCF

**Faster time to value, and faster time to production.** Most of the work in a connector has nothing to do with the source it reads. Before a team can index its first document, it needs compute, schedules, a record of what it already wrote, retries, credential handling, and monitoring. Built from scratch for each source, that takes weeks. CCF provides **managed connector infrastructure** for all of it. Your team spends its time on the one part only it can write, the code that reads your source, and **reaches a searchable knowledge base sooner**.

**Full and incremental syncs, managed for you.** Keeping a knowledge base in step with a changing source is where most connectors get complicated. CCF's API and toolkit handle that work for you. It remembers every document a job wrote, writes only the documents that changed, and removes the ones your code reports are gone. Checkpoints let each job read only what changed since the last one, and pick up where a failed job stopped. Your code says what the source holds, and **CCF manages full and incremental syncs**.

**Your data stays yours.** Documents, knowledge bases, the source's credentials, and connector logs **stay in the AWS account that owns them**, called the tenant account. CCF reaches that account only through roles the tenant creates. It gives connector code no access to CCF's own resources, and it stores records about documents, not their content. [Security](administrator-guide/security.md) describes each boundary.

**You stay in control.** Each custom connector runs one job at a time on the schedule you choose, and your code sets the pace of its requests. You decide **how hard each source's API is used**, so you stay within its rate limits and terms of use. Your code also decides **which documents to include**, which to leave out, which attributes to attach for filtering, and who may see each document. A built-in connector limits those choices to the options it exposes.

**It scales with your sources.** CCF starts compute for each job and releases it when the job ends. Adding a connector, or a larger source, doesn't mean adding servers to size, patch, or keep running between jobs. A large source can take hours to read, and a job can run for up to seven days.

**Simpler than building on the [AWS SDKs](https://aws.amazon.com/developer/tools/).** Your code calls CCF's Python toolkit to **put a document, delete one, or save a checkpoint**. The toolkit signs the API requests and manages the storage keys. A testing module runs your connector on your machine against in-memory fakes, so you can try a change before you deploy it.

**One deployment serves every team.** An administrator hosts CCF once and admits each team's AWS account as a tenant. Every team then uses the same API, toolkit, and deployment constructs, and the administrator monitors every team's jobs through **one set of alarms, one dashboard, and one runbook**.

## Where your documents go

Each custom connector writes to a destination, and the destination's type decides who indexes and searches the documents.

| Destination type | Who indexes it | Who searches it |
| --- | --- | --- |
| `BEDROCK` | An [Amazon Bedrock managed knowledge base](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-build-managed.html), which CCF creates and updates after each successful job | Your applications and agents with `Retrieve`, and AWS Quick, which connects the knowledge base as a knowledge source |
| `QUICK` | AWS Quick, through its [Amazon S3 integration](https://docs.aws.amazon.com/quick/latest/userguide/s3-integration.html) | AWS Quick |
| `S3` | Your own pipeline | Your own pipeline |

## Example: index your GitHub organizations

The repository includes a complete example connector for GitHub. It indexes the public repositories of the GitHub organizations you choose, and updates the index every week.

A large engineering organization writes the same code more than once, because one team can't find what another team already built. With its repositories indexed, anyone can ask whether something exists before they build it, and get the files that do it, with links. The same index serves the coding agents your teams use, so they reuse code instead of writing it again.

The connector is built for large organizations. It skips repositories whose last push hasn't changed, and waits out GitHub's rate limits. It also leaves out the generated SDK repositories that would only add noise. [Inside the GitHub connector](blog/github-connector.md) walks through those choices.

## Get started

| If you want to | Start with |
| --- | --- |
| See how CCF works end to end | [How it works](getting-started/how-it-works.md) |
| Try it in one AWS account | [Tutorial: index GitHub organizations](getting-started/tutorial.md) |
| Host CCF for other teams | [Administrator Guide](administrator-guide/index.md) |
| Build a connector for your source | [Developer Guide](developer-guide/index.md) |
| Search your documents in AWS Quick or from an agent | [Search your documents](developer-guide/search.md) |
| Read why teams adopt it | [Blog](blog/index.md) |

CCF is a sample, licensed under [MIT-0](https://github.com/aws-samples/sample-custom-connector-framework-for-knowledge-bases/blob/main/LICENSE). Review it against your own requirements before you use it with production data.
