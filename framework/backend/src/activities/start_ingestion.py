"""Start the ingestion one queue message asks for.

The message's token is cleared from the destination before Bedrock is called, so a duplicate
message finds no token and does nothing. A retryable failure puts the token back (only when no
newer one is waiting) and delays the message by its receive count; an error a retry can't fix
sends the message to the dead-letter queue.
"""

from __future__ import annotations

from enum import StrEnum

from ccf_api_models.base import ValueModel
from ccf_api_models.identifiers import DestinationRef, TenantContext
from ccf_api_models.models import DestinationStatus
from pydantic import ValidationError

from common.errors import ResourceNotFoundError
from common.ingestion_requests import IngestionRequestMessage, RetryDelays
from common.observability import MetricName, MetricRecord, ServiceMetrics, logger
from common.storage.bedrock.bedrock_ingestion_dao import (
    BedrockIngestionDao,
    IngestionDisposition,
    IngestionStartError,
    StartIngestionRequest,
)
from common.storage.ddb.destinations_dao import DestinationsDao, IngestionTokenRequest
from common.storage.sqs.ingestion_queue_dao import DelayMessageRequest, IngestionQueueDao


class IngestionSkipReason(StrEnum):
    """Why the consumer dropped a message without starting an ingestion job."""

    DESTINATION_DELETED = "destination_deleted"
    DESTINATION_DELETING = "destination_deleting"
    NO_KNOWLEDGE_BASE = "no_knowledge_base"
    DUPLICATE_MESSAGE = "duplicate_message"


class IngestionOutcome(StrEnum):
    """What happened to one message."""

    STARTED = "STARTED"
    SKIPPED = "SKIPPED"
    RETRY = "RETRY"
    DEAD_LETTERED = "DEAD_LETTERED"


class IngestionDelivery(ValueModel):
    """One received message: its raw body, and what the queue needs to delay or drop it."""

    body: str
    message_id: str
    queue_url: str
    receipt_handle: str
    receive_count: int


class StartIngestionActivity:
    """Starts the ingestion job one ingestion request message asks for."""

    def __init__(
        self,
        destinations: DestinationsDao,
        bedrock: BedrockIngestionDao,
        queue: IngestionQueueDao,
        metrics: ServiceMetrics,
        retry_delays: RetryDelays,
    ) -> None:
        self._destinations = destinations
        self._bedrock = bedrock
        self._queue = queue
        self._metrics = metrics
        self._retry_delays = retry_delays

    def start(self, delivery: IngestionDelivery) -> IngestionOutcome:
        """Start the message's ingestion; an unparseable message, or one Bedrock rejects, is dead-lettered."""
        try:
            message = IngestionRequestMessage.model_validate_json(delivery.body)
        except ValidationError:
            logger.error("ingestion_message_unparseable", extra={"message_id": delivery.message_id})
            return self._dead_letter(delivery)
        log_context = {"destination_id": message.destination_id}
        destination = DestinationRef(
            tenant=TenantContext.from_arn_prefix(message.tenant_arn_prefix), destination_id=message.destination_id
        )
        try:
            stored = self._destinations.get(destination)
        except ResourceNotFoundError:
            return self._skipped(
                _Skip(destination_id=message.destination_id, reason=IngestionSkipReason.DESTINATION_DELETED)
            )
        if stored.status is DestinationStatus.DELETING:
            return self._skipped(
                _Skip(destination_id=message.destination_id, reason=IngestionSkipReason.DESTINATION_DELETING)
            )
        resources = stored.destination_resources
        if resources.knowledge_base_id is None or resources.data_source_id is None:
            return self._skipped(
                _Skip(destination_id=message.destination_id, reason=IngestionSkipReason.NO_KNOWLEDGE_BASE)
            )
        token = IngestionTokenRequest(destination=destination, token=message.token)
        if not self._destinations.clear_ingestion_pending(token):
            return self._skipped(
                _Skip(destination_id=message.destination_id, reason=IngestionSkipReason.DUPLICATE_MESSAGE)
            )
        try:
            self._bedrock.start_ingestion(
                StartIngestionRequest(
                    destination_id=stored.destination_id,
                    role_arn=stored.role_arn,
                    knowledge_base_id=resources.knowledge_base_id,
                    data_source_id=resources.data_source_id,
                    client_token=message.token,
                )
            )
        except IngestionStartError as error:
            failure = {**log_context, "error_type": type(error).__name__, "receive_count": delivery.receive_count}
            match error.disposition:
                case IngestionDisposition.RETRY:
                    self._destinations.restore_ingestion_pending(token)
                    delay = self._retry_delays.for_receive(delivery.receive_count)
                    self._queue.delay(
                        DelayMessageRequest(
                            queue_url=delivery.queue_url, receipt_handle=delivery.receipt_handle, delay_seconds=delay
                        )
                    )
                    logger.warning("ingestion_retry_scheduled", extra={**failure, "delay_seconds": delay})
                    return IngestionOutcome.RETRY
                case IngestionDisposition.REJECT:
                    logger.error("ingestion_dead_lettered", extra=failure)
                    return self._dead_letter(delivery)
        self._metrics.record(MetricRecord(name=MetricName.INGESTION_JOBS_STARTED, value=1))
        logger.info("ingestion_started", extra=log_context)
        return IngestionOutcome.STARTED

    def _dead_letter(self, delivery: IngestionDelivery) -> IngestionOutcome:
        self._queue.dead_letter(delivery.body)
        self._metrics.record(MetricRecord(name=MetricName.INGESTION_JOBS_FAILED, value=1))
        return IngestionOutcome.DEAD_LETTERED

    @staticmethod
    def _skipped(skip: _Skip) -> IngestionOutcome:
        logger.info("ingestion_skipped", extra={"destination_id": skip.destination_id, "reason": skip.reason.value})
        return IngestionOutcome.SKIPPED


class _Skip(ValueModel):
    destination_id: str
    reason: IngestionSkipReason
