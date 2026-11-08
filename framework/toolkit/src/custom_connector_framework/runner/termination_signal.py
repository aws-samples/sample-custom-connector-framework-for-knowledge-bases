"""`SIGTERM` handling: Batch sends it on a stop or a timeout and kills the container 30 seconds later."""

from __future__ import annotations

import signal
from types import FrameType


class JobTerminatedError(BaseException):
    """Raised in the main thread when the container receives `SIGTERM`.

    It derives from `BaseException`, so an implementation's `except Exception` doesn't swallow it.
    """


class TerminationSignal:
    """Turns `SIGTERM` into `JobTerminatedError` while `run()` runs, and ignores it during the flush."""

    def arm(self) -> None:
        """Raise `JobTerminatedError` on the next `SIGTERM`."""
        signal.signal(signal.SIGTERM, self._raise)

    def disarm(self) -> None:
        """Ignore `SIGTERM`, so the flush that follows `run()` isn't interrupted."""
        signal.signal(signal.SIGTERM, signal.SIG_IGN)

    @staticmethod
    def _raise(signal_number: int, frame: FrameType | None) -> None:
        raise JobTerminatedError(f"Received signal {signal_number}")
