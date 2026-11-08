"""The ingestion consumer's composition root: its settings and how outcomes reach SQS."""

from __future__ import annotations

import importlib
import sys
from types import ModuleType
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from activities.start_ingestion import IngestionOutcome

BODY = (
    '{"tenant_arn_prefix":"arn:aws:ccf:us-east-1:111122223333","destination_id":"dst-7c41e9a2b5d0",'
    '"token":"ing-3f6c2d1e-0000-4000-8000-000000000000"}'
)


class LambdaContext:
    function_name = "CcfIngestionConsumer"
    memory_limit_in_mb = 256
    invoked_function_arn = "arn:aws:lambda:us-east-1:444455556666:function:CcfIngestionConsumer"
    aws_request_id = "lambda-request-1"


def load() -> ModuleType:
    sys.modules.pop("ingestion_consumer_handler", None)
    return importlib.import_module("ingestion_consumer_handler")


@pytest.fixture
def handler_module(example_environment: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    module = load()
    monkeypatch.setattr(module, "start_ingestion_activity", MagicMock())
    return module


def event(body: str = BODY) -> dict[str, object]:
    return {
        "Records": [
            {
                "messageId": "message-1",
                "receiptHandle": "receipt-1",
                "body": body,
                "attributes": {"ApproximateReceiveCount": "3"},
                "eventSource": "aws:sqs",
                "eventSourceARN": "arn:aws:sqs:us-east-1:444455556666:CcfIngestionQueue",
            }
        ]
    }


@pytest.mark.usefixtures("example_environment")
def test_a_missing_workflow_arn_fails_the_cold_start_naming_the_field(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORKFLOWS__DESTINATION")

    with pytest.raises(ValidationError) as raised:
        load()

    assert [detail["loc"] for detail in raised.value.errors()] == [("workflows", "destination")]


@pytest.mark.parametrize(
    ("outcome", "failures"),
    [
        (IngestionOutcome.STARTED, []),
        (IngestionOutcome.SKIPPED, []),
        (IngestionOutcome.DEAD_LETTERED, []),
        (IngestionOutcome.RETRY, ["message-1"]),
    ],
)
def test_only_a_retry_reports_the_message_failed(
    handler_module: ModuleType, outcome: IngestionOutcome, failures: list[str]
) -> None:
    handler_module.start_ingestion_activity.start.return_value = outcome

    response = handler_module.handler(event(), LambdaContext())

    assert [item["itemIdentifier"] for item in response["batchItemFailures"]] == failures


def test_the_activity_gets_the_receive_count_and_the_queue_the_message_came_from(handler_module: ModuleType) -> None:
    handler_module.start_ingestion_activity.start.return_value = IngestionOutcome.STARTED

    handler_module.handler(event(), LambdaContext())

    delivery = handler_module.start_ingestion_activity.start.call_args.args[0]
    assert (delivery.receive_count, delivery.queue_url) == (
        3,
        "https://sqs.us-east-1.amazonaws.com/444455556666/CcfIngestionQueue",
    )


def test_any_body_reaches_the_activity_raw(handler_module: ModuleType) -> None:
    handler_module.start_ingestion_activity.start.return_value = IngestionOutcome.DEAD_LETTERED

    response = handler_module.handler(event("not json"), LambdaContext())

    assert response["batchItemFailures"] == []
    delivery = handler_module.start_ingestion_activity.start.call_args.args[0]
    assert (delivery.body, delivery.message_id) == ("not json", "message-1")
