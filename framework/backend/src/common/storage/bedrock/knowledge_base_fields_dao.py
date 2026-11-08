"""Sets a knowledge base's custom metadata fields in the tenant's account through its `CcfDestinationRole`.

Bedrock takes the fields only on `UpdateKnowledgeBase`, which also requires the knowledge base's name, role, and
configuration. The call reads them with `GetKnowledgeBase` and sends them back unchanged, with the new fields.
"""

from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError
from ccf_api_models.base import ValueModel
from ccf_api_models.models import MetadataField

from common.aws_error_code import AwsErrorCode
from common.errors import (
    BadRequestError,
    DestinationRoleDeniedError,
    FailedAwsCall,
    ResourceConflictError,
    ServiceError,
)
from common.tenant_role_clients import AssumeTenantRoleRequest, TenantRoleClients

_ACTION = "Knowledge base metadata fields update"


class SetMetadataFieldsRequest(ValueModel):
    """One destination's knowledge base, the role that reaches it, and the fields that replace its own."""

    destination_id: str
    role_arn: str
    knowledge_base_id: str
    fields: tuple[MetadataField, ...]


class MetadataFieldsRejectedError(BadRequestError):
    public_message = "The knowledge base rejected the metadata fields: a name repeats, or a field's type changed"


class KnowledgeBaseUpdatingError(ResourceConflictError):
    public_message = "The knowledge base is still applying an earlier update; retry in a few seconds"


class KnowledgeBaseFieldsDao:
    def __init__(self, clients: TenantRoleClients, external_id: str) -> None:
        self._clients = clients
        self._external_id = external_id

    def set_fields(self, request: SetMetadataFieldsRequest) -> None:
        """Replace the knowledge base's fields; an empty tuple removes them all."""
        session = self._clients.assume(
            AssumeTenantRoleRequest(
                role_arn=request.role_arn,
                external_id=self._external_id,
                session_name=f"ccf-fields-{request.destination_id}",
            )
        )
        bedrock = session.client("bedrock-agent")
        try:
            current: Any = bedrock.get_knowledge_base(knowledgeBaseId=request.knowledge_base_id)["knowledgeBase"]
            bedrock.update_knowledge_base(
                knowledgeBaseId=request.knowledge_base_id,
                name=current["name"],
                roleArn=current["roleArn"],
                knowledgeBaseConfiguration=current["knowledgeBaseConfiguration"],
                metadataConfigurations=[
                    {"name": field.name, "type": field.type.value, "searchability": field.searchability.value}
                    for field in request.fields
                ],
            )
        except ClientError as error:
            raise _error(error, request.role_arn) from error


def _error(error: ClientError, role_arn: str) -> ServiceError:
    code = error.response.get("Error", {}).get("Code")
    match AwsErrorCode.from_code(code):
        case AwsErrorCode.VALIDATION:
            return MetadataFieldsRejectedError("Bedrock rejected the metadata fields", aws_error_code=code)
        case AwsErrorCode.CONFLICT:
            return KnowledgeBaseUpdatingError("The knowledge base is UPDATING", aws_error_code=code)
        case AwsErrorCode.ACCESS_DENIED_EXCEPTION:
            return DestinationRoleDeniedError(role_arn)
        case _:
            return FailedAwsCall(error=error, action=_ACTION).to_error()
