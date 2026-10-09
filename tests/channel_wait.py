"""Bounded observation of asynchronous Channel dispatch in deterministic tests."""

import time
from collections.abc import Callable


def wait_for_channel(predicate: Callable[[], bool], timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), "Channel dispatch did not reach the expected durable state"
