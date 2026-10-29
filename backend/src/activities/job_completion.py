"""Complete one framework Batch execution and start destination ingestion."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from ccf_api_models.models import DestinationMode, JobStatus

from common.batch_job_definition import (
    BatchJobDefinitionCleanup,
    CleanupBatchJobDefinitionRequest,
)
from common.storage.ddb.custom_connector_jobs_dao import (
    BatchTerminalStatus,
    CustomConnectorJobsDao,
    ReconcileBatchJobStatusRequest,
)

if TYPE_CHECKING:
    from mypy_boto3_bedrock_agent import AgentsforBedrockClient


class CompleteBatchJobActivity:
    """Coordinate destination ingestion before the CCF terminal transition."""

    def __init__(
        self,
        jobs_dao: CustomConnectorJobsDao,
        job_definition_cleanup: BatchJobDefinitionCleanup,
        bedrock_agent_client: AgentsforBedrockClient,
        destination_mode: DestinationMode,
        knowledge_base_id: str,
        data_source_id: str,
    ) -> None:
        self._jobs_dao = jobs_dao
        self._job_definition_cleanup = job_definition_cleanup
        self._bedrock_agent_client = bedrock_agent_client
        self._destination_mode = destination_mode
        self._knowledge_base_id = knowledge_base_id
        self._data_source_id = data_source_id

    def complete(self, request: ReconcileBatchJobStatusRequest) -> JobStatus:
        """Start required ingestion and then apply the terminal CCF status."""
        completion_pending = self._jobs_dao.is_batch_completion_pending(request)
        if (
            completion_pending
            and request.batch_status is BatchTerminalStatus.SUCCEEDED
            and self._destination_mode is DestinationMode.BEDROCK
        ):
            self._bedrock_agent_client.start_ingestion_job(
                knowledgeBaseId=self._knowledge_base_id,
                dataSourceId=self._data_source_id,
                clientToken=self._client_token(request),
            )
        terminal_status = self._jobs_dao.reconcile_batch_job_status(request)
        self._job_definition_cleanup.cleanup(
            CleanupBatchJobDefinitionRequest(
                request.tenant_context,
                request.connector_id,
                request.job_id,
            )
        )
        return terminal_status

    @staticmethod
    def _client_token(request: ReconcileBatchJobStatusRequest) -> str:
        identity = ":".join(
            (
                request.tenant_context.get_arn_prefix(),
                request.connector_id,
                request.job_id,
            )
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()
