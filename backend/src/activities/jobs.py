"""One activity per custom-connector job control-plane operation."""

from __future__ import annotations

import hashlib
import uuid

from ccf_api_models.models import (
    GetCustomConnectorJobRequest,
    GetCustomConnectorJobResponseContent,
    JobDetail,
    JobStatus,
    ListCustomConnectorJobsRequest,
    ListCustomConnectorJobsResponseContent,
    StartCustomConnectorJobRequest,
    StartCustomConnectorJobResponseContent,
    StopCustomConnectorJobRequest,
)

from common.configuration import (
    ConfigurationCodec,
    ConfigurationValidator,
    DecryptConfigurationRequest,
    EncryptConfigurationRequest,
)
from common.operation import OperationRequest
from common.storage.ddb.custom_connector_jobs_dao import (
    CustomConnectorJobsDao,
    GetJobRequest,
    ListJobsRequest,
    StartJobRequest,
    UpdateJobStatusRequest,
)


class StartCustomConnectorJobActivity:
    """Validate, encrypt, and create immutable job input."""

    def __init__(
        self,
        dao: CustomConnectorJobsDao,
        validator: ConfigurationValidator,
        codec: ConfigurationCodec,
    ) -> None:
        self._dao = dao
        self._validator = validator
        self._codec = codec

    def start(
        self,
        request: OperationRequest[StartCustomConnectorJobRequest],
    ) -> StartCustomConnectorJobResponseContent:
        """Start one job with ciphertext-only configuration persistence."""
        job_id = self._job_id(request)
        job_arn = request.tenant.get_job_arn(request.payload.connector_id, job_id)
        configuration = self._validator.validate(request.payload.job_configuration)
        encrypted = self._codec.encrypt(EncryptConfigurationRequest(configuration, job_arn))
        job = self._dao.start_job(
            StartJobRequest(
                request.tenant,
                request.payload.connector_id,
                encrypted,
                job_id,
                request.idempotency_key,
            )
        )
        return StartCustomConnectorJobResponseContent(job=job)

    @staticmethod
    def _job_id(request: OperationRequest[StartCustomConnectorJobRequest]) -> str:
        if request.idempotency_key is None:
            return f"ccj-{uuid.uuid4().hex[:12]}"
        identity = ":".join(
            (
                request.tenant.get_arn_prefix(),
                request.payload.connector_id,
                request.idempotency_key,
            )
        )
        return f"ccj-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:32]}"


class GetCustomConnectorJobActivity:
    """Fetch and decrypt one authorized connector job."""

    def __init__(self, dao: CustomConnectorJobsDao, codec: ConfigurationCodec) -> None:
        self._dao = dao
        self._codec = codec

    def fetch(
        self,
        request: OperationRequest[GetCustomConnectorJobRequest],
    ) -> GetCustomConnectorJobResponseContent:
        """Return immutable job configuration after connector isolation checks."""
        stored = self._dao.get_job(
            GetJobRequest(
                request.tenant,
                request.payload.connector_id,
                request.payload.job_id,
            )
        )
        resource_arn = request.tenant.get_job_arn(stored.connector_id, stored.job_id)
        configuration = self._codec.decrypt(DecryptConfigurationRequest(stored.encrypted_configuration, resource_arn))
        return GetCustomConnectorJobResponseContent(
            job=JobDetail(
                job_id=stored.job_id,
                connector_id=stored.connector_id,
                status=stored.status,
                job_configuration=configuration,
                created_at=stored.created_at,
                updated_at=stored.updated_at,
                service_execution=stored.service_execution,
            )
        )


class StopCustomConnectorJobActivity:
    """Request one non-terminal job to stop."""

    def __init__(self, dao: CustomConnectorJobsDao) -> None:
        self._dao = dao

    def stop(self, request: OperationRequest[StopCustomConnectorJobRequest]) -> None:
        """Move a tenant- and connector-scoped job to STOPPING."""
        self._dao.update_job_status(
            UpdateJobStatusRequest(
                request.tenant,
                request.payload.connector_id,
                request.payload.job_id,
                JobStatus.STOPPING,
            )
        )


class ListCustomConnectorJobsActivity:
    """List job summaries for one connector."""

    def __init__(self, dao: CustomConnectorJobsDao) -> None:
        self._dao = dao

    def list(
        self,
        request: OperationRequest[ListCustomConnectorJobsRequest],
    ) -> ListCustomConnectorJobsResponseContent:
        """List jobs without configuration or ciphertext."""
        stored = self._dao.list_jobs(
            ListJobsRequest(
                request.tenant,
                request.payload.connector_id,
                request.payload.max_results or 50,
                request.payload.next_token,
                request.payload.status,
            )
        )
        return ListCustomConnectorJobsResponseContent(
            jobs=stored.jobs,
            next_token=stored.next_token,
        )
