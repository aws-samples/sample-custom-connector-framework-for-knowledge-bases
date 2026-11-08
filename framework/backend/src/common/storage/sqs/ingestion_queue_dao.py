"""Delays an ingestion request message for a retry, or moves it to the dead-letter queue."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ccf_api_models.base import ValueModel

if TYPE_CHECKING:
    from mypy_boto3_sqs import SQSClient


class DelayMessageRequest(ValueModel):
    """One received message and how long it stays hidden before its next delivery."""

    queue_url: str
    receipt_handle: str
    delay_seconds: int


class IngestionQueueDao:
    """The ingestion queue's retry delay and its dead-letter queue."""

    def __init__(self, sqs_client: SQSClient, dead_letter_queue_url: str) -> None:
        self._sqs = sqs_client
        self._dead_letter_queue_url = dead_letter_queue_url

    def delay(self, request: DelayMessageRequest) -> None:
        self._sqs.change_message_visibility(
            QueueUrl=request.queue_url, ReceiptHandle=request.receipt_handle, VisibilityTimeout=request.delay_seconds
        )

    def dead_letter(self, body: str) -> None:
        self._sqs.send_message(QueueUrl=self._dead_letter_queue_url, MessageBody=body)
