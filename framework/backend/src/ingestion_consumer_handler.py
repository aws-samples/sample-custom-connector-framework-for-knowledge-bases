"""SQS entry point that starts the knowledge base ingestions `CcfIngestionQueue` asks for."""

from __future__ import annotations

from typing import Any

import boto3
from aws_lambda_powertools.utilities.batch import BatchProcessor, EventType, process_partial_response
from aws_lambda_powertools.utilities.batch.types import PartialItemFailureResponse
from aws_lambda_powertools.utilities.data_classes.sqs_event import SQSRecord
from aws_lambda_powertools.utilities.typing import LambdaContext

from activities.start_ingestion import IngestionDelivery, IngestionOutcome, StartIngestionActivity
from common.composition import DataStores, DataStoresRequest
from common.ingestion_requests import INGESTION_RETRY_DELAYS
from common.observability import ServiceMetrics, logger, tracer
from common.resource_data_codec import KmsResourceDataCodec
from common.settings import IngestionConsumerSettings
from common.storage.bedrock.bedrock_ingestion_dao import BedrockIngestionDao
from common.storage.sqs.ingestion_queue_dao import IngestionQueueDao
from common.tenant_role_clients import TenantRoleClients


class IngestionRetryScheduledError(Exception):
    """The message was delayed for a retry; reporting it failed keeps it on the queue."""


settings = IngestionConsumerSettings()
# A retried message is already delayed, so reporting it failed is enough; a batch of retries isn't a Lambda error.
processor = BatchProcessor(event_type=EventType.SQS, raise_on_entire_batch_failure=False)
stores = DataStores.build(
    DataStoresRequest(tables=settings.tables, codec=KmsResourceDataCodec.for_key(settings.resource_data_key_arn))
)
start_ingestion_activity = StartIngestionActivity(
    stores.destinations,
    BedrockIngestionDao(TenantRoleClients(boto3.client("sts"), settings.aws_region), settings.workflows.destination),
    IngestionQueueDao(boto3.client("sqs"), settings.ingestion_dead_letter_queue_url),
    ServiceMetrics(),
    INGESTION_RETRY_DELAYS,
)


@tracer.capture_method
def record_handler(record: SQSRecord) -> None:
    """Process one message; a retry raises so the batch reports the message failed."""
    outcome = start_ingestion_activity.start(
        IngestionDelivery(
            body=record.body,
            message_id=record.message_id,
            queue_url=record.queue_url,
            receipt_handle=record.receipt_handle,
            receive_count=int(record.attributes.approximate_receive_count),
        )
    )
    if outcome is IngestionOutcome.RETRY:
        raise IngestionRetryScheduledError(record.message_id)


@logger.inject_lambda_context(clear_state=True)
@tracer.capture_lambda_handler
def handler(event: dict[str, Any], context: LambdaContext) -> PartialItemFailureResponse:
    """Process one batch of ingestion request messages, reporting each one to retry."""
    return process_partial_response(event=event, record_handler=record_handler, processor=processor, context=context)
