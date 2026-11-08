"""How the connector waits before it reads a broken archive again."""

import random
from collections.abc import Callable
from typing import Protocol, runtime_checkable


@runtime_checkable
class Sleeper(Protocol):
    """Waits up to a ceiling: `JitteredSleeper` in a job, a fake in tests. Runtime-checkable, so a model holds one."""

    def sleep_up_to(self, ceiling_seconds: float) -> None: ...


class JitteredSleeper:
    """Sleeps a random time between 0 and a ceiling (full jitter), so retries from many jobs spread out."""

    def __init__(self, sleep: Callable[[float], None], jitter: random.Random) -> None:
        self._sleep = sleep
        self._jitter = jitter

    def sleep_up_to(self, ceiling_seconds: float) -> None:
        """Sleep between 0 and `ceiling_seconds`."""
        self._sleep(self._jitter.uniform(0, ceiling_seconds))
