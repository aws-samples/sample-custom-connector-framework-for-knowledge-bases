"""The scheduled start: one job per scheduled time of a custom connector's schedule.

The client token comes from the custom connector and the scheduled time, so a redelivered event
returns the job the first delivery started instead of starting another.
"""

from __future__ import annotations

import hashlib

from ccf_api_models.base import ValueModel
from ccf_api_models.catalog import OperationSpec
from ccf_api_models.identifiers import TenantContext
from ccf_api_models.models import StartCustomConnectorJobRequest, StartCustomConnectorJobResponseContent

from activities.jobs import StartCustomConnectorJobActivity
from common.idempotency import IdempotentCreates, IdempotentRunRequest
from common.observability import LogContext, logger
from common.operation import OperationRequest, RequestTrace
from common.schedule_lifecycle import ScheduledStartEvent
from common.storage.ddb.custom_connector_jobs_dao import ConnectorInUseError

_CLIENT_TOKEN_HASH_CHARACTERS = 48


class ScheduledClientToken:
    """The client token of one scheduled start, the same for every delivery of one scheduled time."""

    def for_event(self, event: ScheduledStartEvent) -> str:
        """Return `scheduled-` plus 48 hex characters of the SHA-256 of the custom connector and scheduled time."""
        identity = f"{event.tenant_arn_prefix}:{event.destination_id}:{event.connector_id}:{event.scheduled_time}"
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return f"scheduled-{digest[:_CLIENT_TOKEN_HASH_CHARACTERS]}"


class ScheduledStart(ValueModel):
    """One delivered schedule event and the ID of the Lambda invocation that received it."""

    event: ScheduledStartEvent
    invocation_id: str


class ScheduledStartActivity:
    """Starts the scheduled job, or skips the scheduled time while the custom connector's last job still runs."""

    def __init__(
        self,
        jobs: StartCustomConnectorJobActivity,
        idempotency: IdempotentCreates,
        spec: OperationSpec,
        tokens: ScheduledClientToken,
    ) -> None:
        self._jobs = jobs
        self._idempotency = idempotency
        self._spec = spec
        self._tokens = tokens

    def start(self, scheduled: ScheduledStart) -> StartCustomConnectorJobResponseContent | None:
        """Return the started job, the one an earlier delivery started, or None when the scheduled time is skipped."""
        event = scheduled.event
        tenant = TenantContext.from_arn_prefix(event.tenant_arn_prefix)
        log_context = LogContext(
            account_id=tenant.account_id, destination_id=event.destination_id, connector_id=event.connector_id
        ).as_log_extra()
        payload = StartCustomConnectorJobRequest(
            destination_id=event.destination_id,
            connector_id=event.connector_id,
            client_token=self._tokens.for_event(event),
        )
        request: OperationRequest[StartCustomConnectorJobRequest] = OperationRequest(
            tenant=tenant,
            payload=payload,
            trace=RequestTrace(correlation_id=event.correlation_id, api_request_id=scheduled.invocation_id),
        )
        try:
            response = self._idempotency.run(
                IdempotentRunRequest(spec=self._spec, request=request, activity=self._jobs.start)
            )
        except ConnectorInUseError:
            # The previous job is still running; the next scheduled time starts the next one.
            logger.warning("scheduled_job_skipped", extra={**log_context, "reason": "custom connector in use"})
            return None
        started = StartCustomConnectorJobResponseContent.model_validate(response)
        logger.info("scheduled_job_started", extra={**log_context, "job_id": started.job.job_id})
        return started
