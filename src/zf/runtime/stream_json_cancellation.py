"""Thread-safe cancellation of an SDK drain, including provider cleanup."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import threading
from typing import Any


class StreamJsonTurnCancellation:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cancelled: set[str] = set()
        self._active: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Task, threading.Event]] = {}

    def reset(self, role_name: str) -> None:
        with self._lock:
            if role_name in self._active:
                raise RuntimeError(f"provider turn for {role_name!r} is still active")
            self._cancelled.discard(role_name)

    async def drain(self, role_name: str, factory: Callable[[], Awaitable[Any]]) -> Any:
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        assert task is not None
        done = threading.Event()
        with self._lock:
            if role_name in self._cancelled:
                raise asyncio.CancelledError
            self._active[role_name] = (loop, task, done)
        try:
            return await factory()
        finally:
            with self._lock:
                self._active.pop(role_name, None)
            done.set()

    def cancel(self, role_name: str, *, timeout_s: float = 5.0) -> None:
        with self._lock:
            self._cancelled.add(role_name)
            active = self._active.get(role_name)
            if active is None:
                return
            loop, task, done = active
            loop.call_soon_threadsafe(task.cancel)
        if not done.wait(timeout_s):
            raise RuntimeError(f"provider turn for {role_name!r} did not stop")

    def cancel_and_join(self, role_name: str, thread: threading.Thread | None) -> None:
        self.cancel(role_name)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)
            if thread.is_alive():
                raise RuntimeError(f"stream-json dispatch for {role_name!r} did not stop")
