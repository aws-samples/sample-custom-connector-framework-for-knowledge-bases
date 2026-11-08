---
description: Deploy the Custom Connector Framework and the example GitHub connector in one AWS account, index GitHub organizations into an Amazon Bedrock knowledge base, and search them.
---

# Tutorial: index GitHub organizations

In this tutorial, you deploy the Custom Connector Framework (CCF) and the example GitHub connector in one AWS account. You run a job that indexes GitHub repositories into an [Amazon Bedrock knowledge base](https://aws.amazon.com/bedrock/knowledge-bases/), and search the result. You act as both the administrator and the connector developer, so the account is both the service account and the tenant account.

## Prerequisites

Before you start, you need:

- An AWS account, credentials for it in your shell, and a Region where [Amazon Bedrock managed knowledge bases](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-managed-create.html) are available.
- The account and Region bootstrapped for the [AWS Cloud Development Kit (AWS CDK)](https://aws.amazon.com/cdk/) (`npx cdk bootstrap aws://<account>/<region>`).
- [uv](https://docs.astral.sh/uv/getting-started/installation/), Node.js 22 or 24, the [AWS Command Line Interface (AWS CLI)](https://aws.amazon.com/cli/), and [gitleaks](https://github.com/gitleaks/gitleaks), which `make check` uses to scan for secrets.
- A running container builder. The AWS CDK uses Docker. To use [Finch](https://runfinch.com/), run `export CDK_DOCKER=finch` in the shell you deploy from.
- A GitHub personal access token that can read public repositories.

## Step 1: Install and check the code

This step builds every project and runs its tests on your machine.

1. Clone the repository and install every project:

    ```bash
    git clone https://github.com/aws-samples/sample-custom-connector-framework-for-knowledge-bases.git
    cd sample-custom-connector-framework-for-knowledge-bases
    make install
    ```

1. Run the lint, type checks, unit tests, documentation build, and secret scan:

    ```bash
    make check
    ```

    The command ends with exit status 0.

## Step 2: Deploy the framework

As the administrator, you deploy the framework's stacks first, because the destination and connector stacks read their exports.

1. Deploy the framework stacks with the default configuration:

    ```bash
    make -C framework deploy
    ```

    This deploys `CustomConnectorFramework`, then `CustomConnectorFrameworkOps`. The default configuration admits only this account as a tenant, and keeps tables and log groups when a stack is deleted.

1. Subscribe to the alarm topic, so you hear about failures. The topic's ARN is the `AlarmTopicArnExport` output of `CustomConnectorFrameworkOps`:

    ```bash
    aws cloudformation describe-stacks --stack-name CustomConnectorFrameworkOps \
      --query "Stacks[0].Outputs[?OutputKey=='AlarmTopicArnExport'].OutputValue" --output text
    ```

## Step 3: Deploy the GitHub connector

As the connector developer, you deploy a destination, then the connector that writes to it. The destination gets its own stack, so you can delete or replace the connector without losing the index.

The default GitHub configuration, `connectors/github/cdk/config/default.json`, creates a custom connector for each of the `aws-samples`, `awslabs`, and `aws` organizations, and reads up to 300 repositories from each. To try it with less, copy `connectors/github/cdk/config/example-small.json`, which reads five repositories of `aws-samples`. Delete its `framework` setting, which points at a framework in another account. Its `logDelivery` is `false`, so set it to `true` if you want to read the job's logs in step 4.

1. Deploy the GitHub connector's destination stack, which creates the destination through the framework API:

    ```bash
    make -C connectors/github deploy-destination
    ```

    This deploys `CustomConnectorDestinationForGitHub`, a `BEDROCK` destination named `GitHub`. The deployment waits until the destination is `ACTIVE`, which can take several minutes. Note its `DestinationId` output, and set it as `destination.id` in your GitHub configuration file.

1. Deploy the connector:

    ```bash
    make -C connectors/github deploy CONFIG=config/<your file>.json
    ```

    `GitHubCustomConnector` builds the connector image, creates the token secret, and registers one custom connector per owner, named `github-<owner>`. Each gets a weekly schedule, a source-access role, and a log group.

1. Read the outputs you need in the next steps:

    ```bash
    aws cloudformation describe-stacks --stack-name CustomConnectorDestinationForGitHub --query "Stacks[0].Outputs"
    aws cloudformation describe-stacks --stack-name GitHubCustomConnector --query "Stacks[0].Outputs"
    aws cloudformation describe-stacks --stack-name CustomConnectorFramework \
      --query "Stacks[0].Outputs[?OutputKey=='ApiEndpoint'].OutputValue" --output text
    ```

    From the destination stack, note `KnowledgeBaseId` and `DataSourceId`. From `GitHubCustomConnector`, note `GitHubTokenSecretArn` and a connector ID, such as `ConnectorIdawssamples` for the `aws-samples` organization. The last command prints the framework's API endpoint.

1. Store your GitHub token in the [AWS Secrets Manager](https://aws.amazon.com/secrets-manager/) secret. `read -rs` keeps it out of your shell history:

    ```bash
    read -rs GITHUB_TOKEN
    aws secretsmanager put-secret-value --secret-id "<GitHubTokenSecretArn>" \
      --secret-string "$GITHUB_TOKEN"
    unset GITHUB_TOKEN
    ```

## Step 4: Run a job

The framework API takes requests signed with [AWS Signature Version 4](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_sigv.html), and the toolkit's `CcfClient` signs them with your credentials.

1. Export the values from step 3:

    ```bash
    export CCF_ENDPOINT="<ApiEndpoint>"
    export CCF_REGION="<your Region>"
    export DESTINATION_ID="<DestinationId>"
    export CONNECTOR_ID="<ConnectorIdawssamples>"
    ```

1. Start a job and wait until it ends:

    ```bash
    uv run --directory framework/toolkit python - <<'EOF'
    import os
    import time

    import boto3
    from ccf_api_models.models import GetCustomConnectorJobRequest, StartCustomConnectorJobRequest
    from custom_connector_framework import CcfClientSettings, create_ccf_client

    client = create_ccf_client(
        CcfClientSettings(
            endpoint=os.environ["CCF_ENDPOINT"],
            region=os.environ["CCF_REGION"],
            session=boto3.Session(region_name=os.environ["CCF_REGION"]),
        )
    )
    ids = {"destination_id": os.environ["DESTINATION_ID"], "connector_id": os.environ["CONNECTOR_ID"]}
    job = client.call(StartCustomConnectorJobRequest(**ids)).job
    print("started", job.job_id)
    while True:
        job = client.call(GetCustomConnectorJobRequest(**ids, job_id=job.job_id)).job
        if job.status in ("SUCCEEDED", "FAILED", "STOPPED"):
            print(job.status, job.status_reason or "")
            break
        time.sleep(30)
    EOF
    ```

    The job reads each repository's archive, so the first run of a large organization takes a while. It prints `SUCCEEDED` when it ends.

1. If the job prints `FAILED`, its `status_reason` says why. For a failure in the connector code, it names the log group and stream to read, for example `ConnectorImplementationError: see log group /ccf/connectors/github-aws-samples, stream ccj-0fdef5d64630`. Read the stream with:

    ```bash
    aws logs tail /ccf/connectors/github-aws-samples --log-stream-names "<job ID>"
    ```

    [Troubleshoot a job](../developer-guide/troubleshooting.md) lists every `status_reason`.

## Step 5: Search the knowledge base

A successful job ends with the framework asking Amazon Bedrock to index the new documents, so search works once that ingestion finishes.

1. Check that the ingestion finished:

    ```bash
    aws bedrock-agent list-ingestion-jobs --knowledge-base-id "<KnowledgeBaseId>" \
      --data-source-id "<DataSourceId>" --query "ingestionJobSummaries[0].status"
    ```

    The command prints `"COMPLETE"` when the documents are indexed.

1. Search the indexed documents with `Retrieve`:

    ```bash
    aws bedrock-agent-runtime retrieve --knowledge-base-id "<KnowledgeBaseId>" \
      --retrieval-query '{"text": "Step Functions workflow that calls Amazon Bedrock"}' \
      --query "retrievalResults[].{score: score, uri: metadata.source_uri}"
    ```

    Each result is a passage from a repository file, with its score and the file's GitHub URL.

1. Optionally, connect the knowledge base to [AWS Quick](https://aws.amazon.com/quick/), so your team can ask about the code in AWS Quick chat. See [Search with AWS Quick through a Bedrock knowledge base](../developer-guide/search.md#search-with-aws-quick-through-a-bedrock-knowledge-base).

## Step 6: Clean up

Delete the stacks in the reverse order of the deploy, and wait for each one. [AWS CloudFormation](https://aws.amazon.com/cloudformation/) refuses to delete a stack whose exports another stack still imports.

```bash
for stack in GitHubCustomConnector CustomConnectorDestinationForGitHub CustomConnectorFrameworkOps CustomConnectorFramework; do
  aws cloudformation delete-stack --stack-name "$stack"
  aws cloudformation wait stack-delete-complete --stack-name "$stack"
done
```

Deleting `GitHubCustomConnector` deletes each custom connector's documents from the destination. `connectors/github/cdk/config/destination.json` sets `removalPolicy` to `RETAIN`, so deleting `CustomConnectorDestinationForGitHub` keeps the destination, its role, and its index. The default configurations also keep the framework's tables, the image repository, the token secret, and the log groups. To delete them, see [Delete a destination](../developer-guide/destinations.md#delete-a-destination) and [Upgrade and tear down](../administrator-guide/upgrade-and-tear-down.md).

## Next steps

From here, you can go in any of these directions:

- Index your own organizations: [GitHub connector configuration](../github-connector/configuration.md#index-your-own-organizations).
- Build a connector for another source: [Write a connector implementation](../developer-guide/connector-implementation.md).
- Host the framework for other teams: [Administrator Guide](../administrator-guide/index.md).
