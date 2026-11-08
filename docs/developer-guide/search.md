---
description: Search the documents a Custom Connector Framework connector syncs, with Amazon Bedrock Retrieve, AWS Quick, or your own agents built with Strands Agents.
---

# Search your documents

After a job succeeds and ingestion finishes, the documents your connector wrote are searchable. Which services can search them depends on the destination's type.

| You search with | Destination type | How it connects |
| --- | --- | --- |
| [Amazon Bedrock](https://aws.amazon.com/bedrock/) `Retrieve`, from the [AWS CLI](https://aws.amazon.com/cli/), an SDK, or an application | `BEDROCK` | Call the knowledge base by its ID |
| [AWS Quick](https://aws.amazon.com/quick/), through an Amazon Bedrock knowledge base | `BEDROCK` | Connect the managed knowledge base to AWS Quick as a knowledge source |
| AWS Quick, through its [Amazon S3](https://aws.amazon.com/s3/) integration | `QUICK` | Create an AWS Quick knowledge base over the destination bucket |
| Your own agents, such as agents built with [Strands Agents](https://strandsagents.com/) | `BEDROCK` | Give the agent a tool that calls `Retrieve` |

One `BEDROCK` destination serves `Retrieve`, AWS Quick, and your agents at the same time, so it's the type to choose when more than one of them searches. Choose `QUICK` when only AWS Quick searches, and you want AWS Quick to index the bucket itself.

## Search with Amazon Bedrock

Any application with AWS credentials can search a `BEDROCK` destination's knowledge base directly.

1. Check that the last ingestion finished, with the `KnowledgeBaseId` and `DataSourceId` outputs of your destination stack:

    ```bash
    aws bedrock-agent list-ingestion-jobs --knowledge-base-id "<KnowledgeBaseId>" \
      --data-source-id "<DataSourceId>" --query "ingestionJobSummaries[0].status"
    ```

    The command prints `"COMPLETE"` when the documents are indexed.

1. Search with [`Retrieve`](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent-runtime_Retrieve.html):

    ```bash
    aws bedrock-agent-runtime retrieve --knowledge-base-id "<KnowledgeBaseId>" \
      --retrieval-query '{"text": "Step Functions workflow that calls Amazon Bedrock"}' \
      --query "retrievalResults[].{score: score, uri: metadata.source_uri}"
    ```

    Each result is a passage from a document, with its score and the document's source URI.

To narrow a search to documents with an attribute, add a filter. This search returns only files from repositories the example GitHub connector read from the `aws-samples` organization:

```bash
aws bedrock-agent-runtime retrieve --knowledge-base-id "<KnowledgeBaseId>" \
  --retrieval-query '{"text": "Step Functions workflow that calls Amazon Bedrock"}' \
  --retrieval-configuration '{"managedSearchConfiguration": {"filter": {"equals": {"key": "owner", "value": "aws-samples"}}}}'
```

## Search with AWS Quick through a Bedrock knowledge base

AWS Quick can use an [Amazon Bedrock managed knowledge base](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-managed-byo-fmkb.html) as a knowledge source. The knowledge base and your AWS Quick instance must be in the same AWS Region, and you can connect up to 2 managed knowledge bases per AWS Quick instance. AWS Quick passes each user's identity to a knowledge base that has access control lists turned on. A `BEDROCK` destination with `accessControlEnabled` then returns each user only the documents they may see.

1. Find the knowledge base's ARN, `arn:aws:bedrock:<region>:<account>:knowledge-base/<KnowledgeBaseId>`.
1. If AWS Quick runs in another account, attach a resource policy to the knowledge base that grants the AWS Quick service role `bedrock:Retrieve` and `bedrock:GetDocumentContent`, as [Resource policies for managed knowledge bases](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-managed-cross-account.html) describes.
1. In AWS Quick, an administrator adds the knowledge base's ARN under **Admin** > **AWS Resources**.
1. Under **Integrations**, create a knowledge base with **Bedrock knowledge base** as the data source type.
1. Add the knowledge base to a space, so AWS Quick queries it in chat.

[Bring your own Amazon Bedrock managed knowledge base](https://docs.aws.amazon.com/quick/latest/userguide/quick-byo-bedrock-kb.html) in the AWS Quick User Guide has the full steps.

## Search with AWS Quick through Amazon S3

A `QUICK` destination writes its documents and their metadata files in the AWS Quick format, and AWS Quick indexes the bucket itself through its [Amazon S3 integration](https://docs.aws.amazon.com/quick/latest/userguide/s3-integration.html). The integration needs an AWS Quick Enterprise subscription. Cross-account access is supported only within the same AWS Region.

1. Deploy a destination with `"type": "QUICK"`, as [Create a destination](destinations.md) describes. Note its `BucketName` output.
1. An AWS Quick administrator grants AWS Quick access to the bucket, as [Grant Amazon Quick access to Amazon S3 buckets](https://docs.aws.amazon.com/quick/latest/userguide/s3-admin-setup.html#s3-grant-bucket-access) describes.
1. Create a knowledge base from the bucket with the Amazon S3 integration. To use the access control lists your connector writes, turn them on when you connect the bucket.

## Search from your own agents

An agent searches a `BEDROCK` destination the same way an application does, by calling `Retrieve`. This [Strands Agents](https://strandsagents.com/) agent has one tool that searches the knowledge base, and answers from what it finds:

```python
import json

import boto3
from strands import Agent, tool

KNOWLEDGE_BASE_ID = "<KnowledgeBaseId>"
bedrock = boto3.client("bedrock-agent-runtime", region_name="us-east-1")


@tool
def search_code(query: str) -> str:
    """Search the indexed GitHub repositories, and return matching passages with their file URLs."""
    response = bedrock.retrieve(
        knowledgeBaseId=KNOWLEDGE_BASE_ID,
        retrievalQuery={"text": query},
    )
    passages = [
        {"text": result["content"]["text"], "uri": result.get("metadata", {}).get("source_uri", "")}
        for result in response["retrievalResults"]
    ]
    return json.dumps(passages)


agent = Agent(
    tools=[search_code],
    system_prompt="Before you suggest new code, search for existing code that already does it, and cite the files.",
)
agent("Is there an existing sample that ingests documents into a knowledge base on a schedule?")
```

The agent's role needs `bedrock:Retrieve` on the knowledge base. The [blog post on Strands Agents](../blog/strands-agents.md) builds this agent step by step.
