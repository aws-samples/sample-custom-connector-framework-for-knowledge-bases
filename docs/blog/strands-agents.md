---
title: Let Strands Agents find the code your teams already wrote
description: Give an agent built with Strands Agents a search tool over the public repositories of your GitHub organizations, indexed into an Amazon Bedrock knowledge base by the Custom Connector Framework, so it reuses existing code before it writes new code.
---

# Let Strands Agents find the code your teams already wrote

A coding agent writes what you ask for, but it doesn't know what your organization has already built. Ask it for a scheduled ingestion job, and it writes a new one, even when another team in your organization already published one. Each copy is more code to review, secure, and maintain.

In this post, we show how to give an agent built with [Strands Agents](https://strandsagents.com/) a search tool over your GitHub organizations, so it looks for existing code first. The [Custom Connector Framework](../index.md) and its example GitHub connector keep the index current in an [Amazon Bedrock](https://aws.amazon.com/bedrock/) knowledge base. The agent searches it with [`Retrieve`](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent-runtime_Retrieve.html).

![Banner titled "Search before you write", with the line "Your coding agent checks what your teams already built." GitHub organizations connect to the Custom Connector Framework hub, which keeps an Amazon Bedrock knowledge base current. The coding agent, built with Strands Agents, searches that knowledge base before it writes code. Below the hub are reuse code before you write it, deleted code drops out, new repositories on the next job, and search scoped by IAM.](../assets/diagrams/blog-strands-agents.png)

## What the Custom Connector Framework is

The Custom Connector Framework (CCF) is an open source AWS sample that simplifies building, running, and operating document ingestion from any document source into Amazon Bedrock knowledge bases, [AWS Quick](https://aws.amazon.com/quick/), or [Amazon S3](https://aws.amazon.com/s3/). You write the code that reads your source. CCF manages everything else.

For a coding agent, CCF brings **faster time to value**. The example GitHub connector already reads GitHub. CCF provides the **managed connector infrastructure** that runs it, so you start from a working index instead of building an ingestion pipeline.

The agent also gets an index that stays current. CCF manages **full and incremental syncs**, so each job removes the files a repository no longer has and adds the repositories that are new. What the agent finds matches your organizations as of the last job.

The index **stays in your own AWS account**. It's an Amazon Bedrock knowledge base, so your agent searches it under the same [AWS Identity and Access Management (IAM)](https://aws.amazon.com/iam/) permissions as any other AWS resource.

## Solution overview

The GitHub connector reads the public repositories of the organizations you choose, weekly by default. It writes each file to a `BEDROCK` destination, which is an Amazon S3 bucket and the knowledge base that indexes it. Each file carries attributes such as `owner`, `repository`, and `path`, so a search can be narrowed to one organization.

The agent gets one tool, `search_code`, which calls `Retrieve` and returns the matching passages with their GitHub URLs. Its system prompt tells it to search before it writes code, and to cite what it found. The index picks up new repositories on the next job, so nobody has to re-index them for the agent to find them.

## Prerequisites

To follow along, you need:

- The framework and the GitHub connector deployed, with at least one successful job, as the [tutorial](../getting-started/tutorial.md) describes. Note the `KnowledgeBaseId` output of the destination stack.
- Python 3.10 or later, and credentials whose role allows `bedrock:Retrieve` on the knowledge base and can invoke the model your agent uses. Strands Agents uses a model in Amazon Bedrock by default. [Model providers](https://strandsagents.com/latest/documentation/docs/user-guide/concepts/model-providers/amazon-bedrock/) describes its access requirements.

## Step 1: Install Strands Agents

The agent needs the Strands Agents SDK, which runs the agent loop, and the [AWS SDK for Python (Boto3)](https://aws.amazon.com/sdk-for-python/), which calls Amazon Bedrock. Install both in a virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate
pip install strands-agents boto3
```

## Step 2: Write the search tool

A Strands Agents tool is a Python function with the `@tool` decorator. The agent reads its docstring to decide when to call it. Save this as `code_agent.py`, and replace `<KnowledgeBaseId>` and `us-east-1` with your knowledge base's ID and AWS Region:

```python
import json

import boto3
from strands import Agent, tool

KNOWLEDGE_BASE_ID = "<KnowledgeBaseId>"
bedrock = boto3.client("bedrock-agent-runtime", region_name="us-east-1")


@tool
def search_code(query: str, owner: str = "") -> str:
    """Search the indexed GitHub repositories, optionally in one organization, and return passages with file URLs."""
    configuration = {"managedSearchConfiguration": {"filter": {"equals": {"key": "owner", "value": owner}}}}
    response = bedrock.retrieve(
        knowledgeBaseId=KNOWLEDGE_BASE_ID,
        retrievalQuery={"text": query},
        **({"retrievalConfiguration": configuration} if owner else {}),
    )
    passages = [
        {"text": result["content"]["text"], "uri": result.get("metadata", {}).get("source_uri", "")}
        for result in response["retrievalResults"]
    ]
    return json.dumps(passages)
```

The `owner` filter uses the `owner` attribute the GitHub connector writes on every file. You can filter on any attribute a document has.

## Step 3: Create the agent

Add the agent to the same file. Its system prompt makes the search the first step:

```python
agent = Agent(
    tools=[search_code],
    system_prompt=(
        "You help engineers build software. Before you write new code, search for existing code that already does it. "
        "When you find a match, recommend it with its URL, and explain how to reuse it. Write new code only when nothing fits."
    ),
)

result = agent("I need a Step Functions workflow that starts an Amazon Bedrock ingestion job on a schedule. Does one exist?")

print(result)
```

## Step 4: Run it

Run the script:

```bash
python code_agent.py
```

The agent calls `search_code` with a query it writes from your request. It reads the passages and answers with the repositories and files that match, each with its GitHub URL. When nothing matches, the system prompt tells it to say so and write new code.

## Clean up

The agent creates no AWS resources. To stop indexing, delete the connector and destination stacks, as the tutorial's [clean-up step](../getting-started/tutorial.md#step-6-clean-up) describes.

## Conclusion

In this post, we showed how to give your coding agent a search tool over your GitHub organizations. An agent that searches your organizations' code before it writes new code recommends what your teams already built, reviewed, and run. CCF keeps that index current, so you don't run a re-indexing job of your own. The same knowledge base can also serve the people on those teams through [AWS Quick](aws-quick.md).

The GitHub connector is one example of a source CCF can index. To index a source of your own, read [Build a custom connector for any document source](build-a-connector.md).
