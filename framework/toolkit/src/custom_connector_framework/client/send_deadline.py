"""A time after which the transport starts no new call attempt and no retry wait.

The job runtime starts it when the container receives `SIGTERM`, because Batch kills the
container 30 seconds later and the flush that follows must end before that.
"""

from __future__ import annotations

from collections.abc import Callable


class SendDeadlineExceededError(Exception):
    """The next call attempt or retry wait would end after the send deadline."""


class SendDeadline:
    """Unset until `start`; afterwards `check` refuses anything that would end after the deadline."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._ends_at: float | None = None

    def start(self, seconds: float) -> None:
        """Set the deadline `seconds` from now."""
        self._ends_at = self._clock() + seconds

    def check(self, seconds_needed: float) -> None:
        """Raise SendDeadlineExceededError when `seconds_needed` from now is past a started deadline."""
        if self._ends_at is not None and self._clock() + seconds_needed > self._ends_at:
            raise SendDeadlineExceededError("The send deadline after SIGTERM has passed")
