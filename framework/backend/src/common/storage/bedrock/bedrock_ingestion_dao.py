"""Starts a knowledge base ingestion in the tenant's account through its `CcfDestinationRole`.

The framework holds no Bedrock permission on tenant resources: each call assumes the destination's role and
builds the Bedrock client from that session. Every failure raises `IngestionStartError`, whose disposition
says whether a later delivery can succeed.
"""

from __future__ import annotations

from enum import StrEnum
from http import HTTPStatus

from botocore.exceptions import BotoCoreError, ClientError
from ccf_api_models.base import ValueModel

from common.aws_error_code import AwsErrorCode
from common.errors import ServiceError, TenantRoleAccessDeniedError
from common.tenant_role_clients import AssumeTenantRoleRequest, TenantRoleClients


class IngestionDisposition(StrEnum):
    RETRY = "RETRY"
    """A later delivery can succeed: throttling, a conflict, a server error, or a network failure."""
    REJECT = "REJECT"
    """No retry can succeed: the role or the knowledge base refuses the call or is gone."""


class IngestionFailure(ValueModel):
    disposition: IngestionDisposition
    aws_error_code: str | None


class IngestionStartError(Exception):
    def __init__(self, failure: IngestionFailure) -> None:
        super().__init__(f"StartIngestionJob failed ({failure.aws_error_code}): {failure.disposition}")
        self.disposition = failure.disposition
        self.aws_error_code = failure.aws_error_code


class StartIngestionRequest(ValueModel):
    """One destination's knowledge base and data source, the role that reaches them, and the client token."""

    destination_id: str
    role_arn: str
    knowledge_base_id: str
    data_source_id: str
    client_token: str


class BedrockIngestionDao:
    """Calls `StartIngestionJob` with a client token, so a repeated request starts one ingestion."""

    def __init__(self, clients: TenantRoleClients, external_id: str) -> None:
        self._clients = clients
        self._external_id = external_id

    def start_ingestion(self, request: StartIngestionRequest) -> None:
        try:
            session = self._clients.assume(
                AssumeTenantRoleRequest(
                    role_arn=request.role_arn,
                    external_id=self._external_id,
                    session_name=f"ccf-ingest-{request.destination_id}",
                )
            )
            session.client("bedrock-agent").start_ingestion_job(
                knowledgeBaseId=request.knowledge_base_id,
                dataSourceId=request.data_source_id,
                clientToken=request.client_token,
            )
        except TenantRoleAccessDeniedError as error:
            failure = IngestionFailure(
                disposition=IngestionDisposition.REJECT, aws_error_code=AwsErrorCode.ACCESS_DENIED
            )
            raise IngestionStartError(failure) from error
        except ServiceError as error:
            failure = IngestionFailure(disposition=IngestionDisposition.RETRY, aws_error_code=error.aws_error_code)
            raise IngestionStartError(failure) from error
        except ClientError as error:
            raise IngestionStartError(_bedrock_failure(error)) from error
        except BotoCoreError as error:
            raise IngestionStartError(
                IngestionFailure(disposition=IngestionDisposition.RETRY, aws_error_code=None)
            ) from error


def _bedrock_failure(error: ClientError) -> IngestionFailure:
    """Reject a refusal below status 500 that names the request; retry everything else."""
    code = error.response.get("Error", {}).get("Code")
    status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", HTTPStatus.INTERNAL_SERVER_ERROR)
    match AwsErrorCode.from_code(code):
        case AwsErrorCode.ACCESS_DENIED_EXCEPTION | AwsErrorCode.RESOURCE_NOT_FOUND | AwsErrorCode.VALIDATION if (
            status < HTTPStatus.INTERNAL_SERVER_ERROR
        ):
            disposition = IngestionDisposition.REJECT
        case _:
            disposition = IngestionDisposition.RETRY
    return IngestionFailure(disposition=disposition, aws_error_code=code)
