"""Each log line carries the job's stamped fields, which no `extra=` field can replace."""

from __future__ import annotations

import json
import logging

from custom_connector_framework.log_lines import JobLogFields, JsonLineFormatter

FIELDS = JobLogFields(job_id="ccj-0fdef5d64630", connector_id="cc-e020d65d76c0")


def formatted(extra: dict[str, object]) -> dict[str, object]:
    record = logging.makeLogRecord({"name": "connector", "levelname": "INFO", "msg": "repository_read", **extra})
    line: dict[str, object] = json.loads(JsonLineFormatter(FIELDS).format(record))
    return line


def test_a_line_carries_the_job_and_connector_ids_and_the_toolkit_service() -> None:
    line = formatted({"repository_count": 3})

    assert (line["job_id"], line["connector_id"], line["service"]) == (
        "ccj-0fdef5d64630",
        "cc-e020d65d76c0",
        "custom-connector-toolkit",
    )
    assert line["repository_count"] == 3


def test_an_extra_field_named_like_a_stamped_field_does_not_replace_it() -> None:
    line = formatted({"job_id": "spoofed", "connector_id": "spoofed", "service": "spoofed", "timestamp": "spoofed"})

    assert (line["job_id"], line["connector_id"], line["service"]) == (
        "ccj-0fdef5d64630",
        "cc-e020d65d76c0",
        "custom-connector-toolkit",
    )
    assert line["timestamp"] != "spoofed"
