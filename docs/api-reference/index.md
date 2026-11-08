---
description: How to call the Custom Connector Framework API, sign requests, and which callers may call each operation.
---

# API Reference

The Custom Connector Framework API manages destinations, custom connectors, jobs, and the documents, collections, and checkpoints a job writes. Tenants call it to set up and run connectors, and each job calls it, through the toolkit, to record what it wrote. The [Smithy model](https://github.com/aws-samples/sample-custom-connector-framework-for-knowledge-bases/tree/main/framework/api) in `framework/api` defines every operation and shape.

- [Operations](operations.md) lists each operation's request and response.
- [Errors](errors.md) lists the errors every operation can return.

## Endpoint and signing

The API's endpoint is the `ApiEndpoint` output of the `CustomConnectorFramework` stack, `https://<API ID>.execute-api.<region>.amazonaws.com/ccf`. Paths start with `/api/v1/destinations`. The API runs on [Amazon API Gateway](https://aws.amazon.com/api-gateway/), so every request must be signed with [AWS Signature Version 4](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_sigv.html) for the `execute-api` service.

## Call the API with the toolkit

The toolkit's `CcfClient` signs each call with a boto3 session's credentials. It fills in `client_token` when you leave it out. It retries a throttled call, and retries a server error only when the call is safe to repeat.

```python
import boto3
from ccf_api_models.models import ListDestinationsRequest
from custom_connector_framework import CcfClientSettings, create_ccf_client

client = create_ccf_client(
    CcfClientSettings(
        endpoint="https://abcdef1234.execute-api.us-east-1.amazonaws.com/ccf/",
        region="us-east-1",
        session=boto3.Session(),
    )
)
for destination in client.list_destinations(ListDestinationsRequest()).destinations:
    print(destination.destination_id, destination.name, destination.status)
```

Each client method takes the operation's request model from `ccf_api_models.models` and returns its response model. An error raises `CcfApiError`, whose `code` is one of the [error codes](errors.md).

To call the API without the toolkit, sign each request with any Signature Version 4 signer, such as `botocore.auth.SigV4Auth`, and send the JSON bodies the [Operations](operations.md) page describes.

## Callers

The API admits these callers, and refuses every other caller with `UnauthorizedException`.

- A **tenant principal** is any [IAM](https://aws.amazon.com/iam/) principal in the service account, or in an account the framework's `allowedTenantAccountIds` lists. It acts for its own account's tenant.
- A **runtime role** is a custom connector's own job, calling through the toolkit. It acts only for its own custom connector.

| Operation | Tenant principal | Runtime role |
| --- | --- | --- |
| `CreateDestination`, `ListDestinations`, `UpdateDestination`, `DeleteDestination` | Yes | No |
| `GetDestination` | Yes | Yes |
| `CreateCustomConnector`, `ListCustomConnectors`, `UpdateCustomConnector`, `DeleteCustomConnector` | Yes | No |
| `GetCustomConnector` | Yes | Yes |
| `StartCustomConnectorJob`, `ListCustomConnectorJobs`, `StopCustomConnectorJob` | Yes | No |
| `GetCustomConnectorJob` | Yes | Yes |
| `BatchPutCustomConnectorDocuments`, `BatchDeleteCustomConnectorDocuments`, `BatchDeleteCustomConnectorCollections` | No | Yes |
| `ListCustomConnectorDocuments`, `ListCustomConnectorCollections` | Yes | Yes |
| `PutCustomConnectorCheckpoint`, `GetCustomConnectorCheckpoint` | Yes | Yes |
| `DeleteCustomConnectorCheckpoint` | Yes | No |

Every call also checks that the resources in its path belong to the caller's tenant and parent. A resource that belongs to another tenant returns `ResourceNotFoundException`, so knowing another tenant's resource ID gives a caller no access to it.

## Idempotency

`CreateDestination`, `CreateCustomConnector`, and `StartCustomConnectorJob` take an optional `client_token`. Repeating a call with the same token returns the first response. A different request with the same token, or a repeat while the first call still runs, returns `ConflictException`. A call without a token is never deduplicated. The other writes name the resource they change, so repeating one has the same effect.
