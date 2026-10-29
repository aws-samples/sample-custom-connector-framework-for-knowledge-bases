# Non-production Bedrock deployment and smoke test

This procedure deploys `CcfDevStack` with `provider=bedrock`, registers
`SampleWebCrawlerStack` from a separate non-production client account, starts one
job through `CcfClient`, and checks that [Amazon Bedrock](https://aws.amazon.com/bedrock/)
ingestion started after [AWS Batch](https://aws.amazon.com/batch/)
completion. It is a procedure only; this repository does not claim that it has
run.

The deployment creates billable AWS resources. Use non-production accounts and
least-privilege deployment roles. Review `cdk diff` before each deploy. Do not
reuse production profiles or account IDs.

## Required values

Set these shell variables before running commands:

```bash
export FRAMEWORK_PROFILE="nonprod-framework"
export CLIENT_PROFILE="nonprod-client"
export AWS_REGION="us-east-1"
export FRAMEWORK_ACCOUNT_ID="111122223333"
export CLIENT_ACCOUNT_ID="444455556666"
export SOURCE_ACCESS_EXTERNAL_ID="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
```

Confirm both profile targets before deploying:

```bash
aws sts get-caller-identity --profile "$FRAMEWORK_PROFILE"
aws sts get-caller-identity --profile "$CLIENT_PROFILE"
```

The framework deploy requires `env=dev`, `provider=bedrock`, and
`allowedClientAccountIds=$CLIENT_ACCOUNT_ID`. The sample deploy requires these
contexts exactly:

- `sample=web-crawler`
- `apiEndpoint`
- `frameworkRestApiId`
- `frameworkAccount`
- `frameworkExecutionPrincipalArn`
- `frameworkRegion`
- `sourceAccessExternalId`

## Deploy the framework

Run from `cdk/`:

```bash
AWS_PROFILE="$FRAMEWORK_PROFILE" \
CDK_DEFAULT_ACCOUNT="$FRAMEWORK_ACCOUNT_ID" \
CDK_DEFAULT_REGION="$AWS_REGION" \
npx cdk diff CcfDevStack \
  --context env=dev \
  --context provider=bedrock \
  --context allowedClientAccountIds="$CLIENT_ACCOUNT_ID"

AWS_PROFILE="$FRAMEWORK_PROFILE" \
CDK_DEFAULT_ACCOUNT="$FRAMEWORK_ACCOUNT_ID" \
CDK_DEFAULT_REGION="$AWS_REGION" \
npx cdk deploy CcfDevStack \
  --context env=dev \
  --context provider=bedrock \
  --context allowedClientAccountIds="$CLIENT_ACCOUNT_ID" \
  --outputs-file cdk.out/framework-outputs.json
```

The required framework outputs are `ServiceEndpoint`, `ServiceRestApiId`, and
`ServiceExecutionPrincipalArn`. Export them without parsing the endpoint:

```bash
export CCF_ENDPOINT="$(jq -r '.CcfDevStack.ServiceEndpoint' cdk.out/framework-outputs.json)"
export CCF_REST_API_ID="$(jq -r '.CcfDevStack.ServiceRestApiId' cdk.out/framework-outputs.json)"
export CCF_EXECUTION_PRINCIPAL_ARN="$(jq -r '.CcfDevStack.ServiceExecutionPrincipalArn' cdk.out/framework-outputs.json)"
```

Resolve the Bedrock IDs and record the newest ingestion job before the smoke run:

```bash
export BEDROCK_KNOWLEDGE_BASE_ID="$(
  aws bedrock-agent list-knowledge-bases \
    --profile "$FRAMEWORK_PROFILE" \
    --region "$AWS_REGION" \
    --query "knowledgeBaseSummaries[?name=='CcfKnowledgeBase'].knowledgeBaseId | [0]" \
    --output text
)"
export BEDROCK_DATA_SOURCE_ID="$(
  aws bedrock-agent list-data-sources \
    --profile "$FRAMEWORK_PROFILE" \
    --region "$AWS_REGION" \
    --knowledge-base-id "$BEDROCK_KNOWLEDGE_BASE_ID" \
    --query "dataSourceSummaries[?name=='CcfKnowledgeBaseDataSource'].dataSourceId | [0]" \
    --output text
)"
export PREVIOUS_INGESTION_JOB_ID="$(
  aws bedrock-agent list-ingestion-jobs \
    --profile "$FRAMEWORK_PROFILE" \
    --region "$AWS_REGION" \
    --knowledge-base-id "$BEDROCK_KNOWLEDGE_BASE_ID" \
    --data-source-id "$BEDROCK_DATA_SOURCE_ID" \
    --sort-by attribute=STARTED_AT,order=DESCENDING \
    --max-results 1 \
    --query 'ingestionJobSummaries[0].ingestionJobId' \
    --output text
)"
```

If either Bedrock ID is empty or `None`, stop. The framework deployment did not
produce the expected knowledge base and data source.

## Deploy the crawler client stack

The client app creates both the retained image foundation and the selected sample
stack. Deploy the foundation first because CDK publishes the sample image to its
repository before CloudFormation creates sample resources.

Run the following from `cdk/`, first with `diff` in place of `deploy` to review
each stack:

```bash
AWS_PROFILE="$CLIENT_PROFILE" \
CDK_DEFAULT_ACCOUNT="$CLIENT_ACCOUNT_ID" \
CDK_DEFAULT_REGION="$AWS_REGION" \
npx cdk deploy ClientImageAssetsFoundationStack \
  --context sample=web-crawler \
  --context apiEndpoint="$CCF_ENDPOINT" \
  --context frameworkRestApiId="$CCF_REST_API_ID" \
  --context frameworkAccount="$FRAMEWORK_ACCOUNT_ID" \
  --context frameworkExecutionPrincipalArn="$CCF_EXECUTION_PRINCIPAL_ARN" \
  --context frameworkRegion="$AWS_REGION" \
  --context sourceAccessExternalId="$SOURCE_ACCESS_EXTERNAL_ID"

AWS_PROFILE="$CLIENT_PROFILE" \
CDK_DEFAULT_ACCOUNT="$CLIENT_ACCOUNT_ID" \
CDK_DEFAULT_REGION="$AWS_REGION" \
npx cdk deploy SampleWebCrawlerStack \
  --context sample=web-crawler \
  --context apiEndpoint="$CCF_ENDPOINT" \
  --context frameworkRestApiId="$CCF_REST_API_ID" \
  --context frameworkAccount="$FRAMEWORK_ACCOUNT_ID" \
  --context frameworkExecutionPrincipalArn="$CCF_EXECUTION_PRINCIPAL_ARN" \
  --context frameworkRegion="$AWS_REGION" \
  --context sourceAccessExternalId="$SOURCE_ACCESS_EXTERNAL_ID" \
  --outputs-file cdk.out/web-crawler-outputs.json
```

The sample outputs are `ConnectorId` and `ConnectorImageUri`. Export the ID:

```bash
export CONNECTOR_ID="$(jq -r '.SampleWebCrawlerStack.ConnectorId' cdk.out/web-crawler-outputs.json)"
```

## Start and poll the CCF job

The caller needs `execute-api:Invoke` for the exact start and get-job routes on
`$CCF_REST_API_ID`. Run from the repository root with a non-production profile
that has those identity permissions and belongs to an account admitted by the
API resource policy:

```bash
AWS_PROFILE="$CLIENT_PROFILE" \
CCF_ENDPOINT="$CCF_ENDPOINT" \
AWS_REGION="$AWS_REGION" \
CONNECTOR_ID="$CONNECTOR_ID" \
uv run --directory toolkit python - <<'PY'
import os
import time

from ccf_api_models.models import (
    GetCustomConnectorJobRequest,
    JobStatus,
    StartCustomConnectorJobRequest,
)
from custom_connector_framework import CcfClient
from pydantic import SecretStr

client = CcfClient(
    endpoint=os.environ["CCF_ENDPOINT"],
    region=os.environ["AWS_REGION"],
)
connector_id = os.environ["CONNECTOR_ID"]
started = client.start_custom_connector_job(
    StartCustomConnectorJobRequest(
        connector_id=connector_id,
        job_configuration=SecretStr("{}"),
    )
)
job_id = started.job.job_id
print(f"job_id={job_id}")

terminal = {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.STOPPED}
deadline = time.monotonic() + 2700
while time.monotonic() < deadline:
    job = client.get_custom_connector_job(
        GetCustomConnectorJobRequest(connector_id=connector_id, job_id=job_id)
    ).job
    print(f"ccf_status={job.status.value}")
    if job.status in terminal:
        if job.status is not JobStatus.SUCCEEDED:
            raise RuntimeError(f"CCF job ended with {job.status.value}")
        break
    time.sleep(15)
else:
    raise TimeoutError("CCF job did not reach a terminal state within 45 minutes")
PY
```

A CCF `SUCCEEDED` status means the connector Batch job succeeded and the
job-status handler accepted the Bedrock `StartIngestionJob` call. Bedrock
continues ingestion asynchronously.

## Verify Bedrock ingestion

Find the new ingestion job. Its ID must differ from the value recorded before the
CCF job:

```bash
export INGESTION_JOB_ID="$(
  aws bedrock-agent list-ingestion-jobs \
    --profile "$FRAMEWORK_PROFILE" \
    --region "$AWS_REGION" \
    --knowledge-base-id "$BEDROCK_KNOWLEDGE_BASE_ID" \
    --data-source-id "$BEDROCK_DATA_SOURCE_ID" \
    --sort-by attribute=STARTED_AT,order=DESCENDING \
    --max-results 1 \
    --query 'ingestionJobSummaries[0].ingestionJobId' \
    --output text
)"

test "$INGESTION_JOB_ID" != "None"
test "$INGESTION_JOB_ID" != "$PREVIOUS_INGESTION_JOB_ID"
```

Poll Bedrock until the new ingestion reaches `COMPLETE` or a terminal failure:

```bash
aws bedrock-agent get-ingestion-job \
  --profile "$FRAMEWORK_PROFILE" \
  --region "$AWS_REGION" \
  --knowledge-base-id "$BEDROCK_KNOWLEDGE_BASE_ID" \
  --data-source-id "$BEDROCK_DATA_SOURCE_ID" \
  --ingestion-job-id "$INGESTION_JOB_ID" \
  --query 'ingestionJob.{id:ingestionJobId,status:status,statistics:statistics,failureReasons:failureReasons}'
```

Repeat that read until `status` is `COMPLETE`. Treat `FAILED`, `STOPPED`, or no
new ingestion ID as a failed smoke test. Record the framework and sample stack
IDs, CCF job ID, Bedrock ingestion job ID, final status, command timestamps, and
redacted command output. Do not record connector configuration, document content,
credentials, or the external ID.
