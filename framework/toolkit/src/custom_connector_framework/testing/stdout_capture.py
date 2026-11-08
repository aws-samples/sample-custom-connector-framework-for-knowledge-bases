"""Captures what a run writes to stdout, and reads each line back as a log event."""

from __future__ import annotations

import io
import logging
import sys
from types import TracebackType
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, ValidationError

from custom_connector_framework.log_lines import JobLogFields, JsonLineFormatter


class LogEvent(BaseModel):
    """One line a run wrote to stdout: a log record, or a printed line with no level or logger.

    The toolkit's own records set the typed members; read an implementation's own members from
    `line`, the JSON line as written.
    """

    model_config = ConfigDict(frozen=True)

    message: str
    line: str
    level: str | None = None
    logger: str | None = None
    job_id: str | None = None
    document_id: str | None = None
    collection_id: str | None = None
    reason: str | None = None
    document_count: int | None = None
    stack_trace: str | None = None


class _LogLine(BaseModel):
    model_config = ConfigDict(extra="ignore")

    timestamp: str
    level: str
    logger: str
    message: str
    job_id: str | None = None
    document_id: str | None = None
    collection_id: str | None = None
    reason: str | None = None
    document_count: int | None = None
    stack_trace: str | None = None


class StdoutCapture:
    """Captures what a run writes to stdout: log records (as JSON lines) and printed lines.

    While open, it is the root logger's only handler and replaces `sys.stdout`; closing it
    restores both.
    """

    def __init__(self, fields: JobLogFields) -> None:
        self.stream = io.StringIO()
        self._fields = fields
        self._saved_stdout: Any = None
        self._saved_handlers: list[logging.Handler] = []
        self._saved_level = logging.NOTSET

    def __enter__(self) -> Self:
        root = logging.getLogger()
        self._saved_stdout = sys.stdout
        self._saved_handlers = list(root.handlers)
        self._saved_level = root.level
        handler = logging.StreamHandler(self.stream)
        handler.setFormatter(JsonLineFormatter(self._fields))
        for existing in self._saved_handlers:
            root.removeHandler(existing)
        root.addHandler(handler)
        root.setLevel(logging.INFO)
        sys.stdout = self.stream
        return self

    def __exit__(
        self, error_type: type[BaseException] | None, error: BaseException | None, trace: TracebackType | None
    ) -> None:
        root = logging.getLogger()
        for existing in list(root.handlers):
            root.removeHandler(existing)
        for saved in self._saved_handlers:
            root.addHandler(saved)
        root.setLevel(self._saved_level)
        sys.stdout = self._saved_stdout

    def log_events(self) -> tuple[LogEvent, ...]:
        """Every line, in order."""
        return tuple(self._event(line) for line in self.stream.getvalue().splitlines() if line)

    @staticmethod
    def _event(line: str) -> LogEvent:
        try:
            parsed = _LogLine.model_validate_json(line)
        except ValidationError:
            return LogEvent(message=line, line=line)
        return LogEvent(line=line, **parsed.model_dump(exclude={"timestamp"}))
