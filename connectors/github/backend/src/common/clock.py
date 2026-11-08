"""The clock the connector reads the current time from."""

from datetime import UTC, datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """The current time: `UtcClock` in a job, a fixed clock in tests. Runtime-checkable, so a model can hold one."""

    def now(self) -> datetime: ...


class UtcClock:
    """Returns the current UTC time."""

    def now(self) -> datetime:
        return datetime.now(UTC)
