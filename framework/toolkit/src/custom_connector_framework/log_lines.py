"""Log records as one JSON object per line on stdout.

The launcher makes this the root logger's only handler, so the toolkit's records, the
implementation's, and every third-party library's reach stdout the same way. The toolkit
routes nothing and calls no log service: the job definition decides where stdout goes.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from io import TextIOBase

from pydantic import BaseModel, ConfigDict

_RECORD_ATTRIBUTES = frozenset(vars(logging.makeLogRecord({}))) | {"message", "asctime", "taskName"}
"""Attributes every `LogRecord` has; anything else on a record came from `extra=` and is written as a field."""

TOOLKIT_SERVICE_NAME = "custom-connector-toolkit"
"""The `service` field of every toolkit log line."""


class JobLogFields(BaseModel):
    """The IDs every line carries; None until the launcher has read them."""

    model_config = ConfigDict(frozen=True)

    job_id: str | None = None
    connector_id: str | None = None


class JsonLineFormatter(logging.Formatter):
    """Formats a record as `timestamp`, `level`, `logger`, `message`, `job_id`, `connector_id`, and its fields."""

    def __init__(self, fields: JobLogFields) -> None:
        super().__init__()
        self._fields = fields

    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, object] = {
            name: value for name, value in vars(record).items() if name not in _RECORD_ATTRIBUTES
        }
        # The stamped fields go last, so an `extra=` field can't replace them.
        line.update(
            {
                "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
                "job_id": self._fields.job_id,
                "connector_id": self._fields.connector_id,
                "service": TOOLKIT_SERVICE_NAME,
            }
        )
        if record.exc_info is not None:
            line["stack_trace"] = self.formatException(record.exc_info)
        return json.dumps(line, default=str)


class InstallLogLinesRequest(BaseModel):
    """The IDs to stamp on every line and the stream to write to."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    fields: JobLogFields
    stream: TextIOBase


def install_log_lines(request: InstallLogLinesRequest) -> None:
    """Make a JSON-line handler on the stream the root logger's only handler, at INFO."""
    handler = logging.StreamHandler(request.stream)
    handler.setFormatter(JsonLineFormatter(request.fields))
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(logging.INFO)
