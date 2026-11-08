"""The archive re-read's delay is fully jittered."""

import random

from common.sleeper import JitteredSleeper


def _seeded() -> random.Random:
    """A seeded generator, so the delays repeat; it picks retry delays, not secrets."""
    return random.Random(7)  # noqa: S311


def test_each_delay_slept_is_between_0_and_the_ceiling_and_they_differ() -> None:
    slept: list[float] = []
    sleeper = JitteredSleeper(sleep=slept.append, jitter=_seeded())

    for _ in range(200):
        sleeper.sleep_up_to(4.0)

    assert len(slept) == 200
    assert all(0.0 <= delay <= 4.0 for delay in slept)
    assert len(set(slept)) > 1


def test_a_zero_ceiling_sleeps_zero() -> None:
    slept: list[float] = []

    JitteredSleeper(sleep=slept.append, jitter=_seeded()).sleep_up_to(0.0)

    assert slept == [0.0]
