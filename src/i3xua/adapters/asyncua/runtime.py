"""Worker-thread pool. Each named thread owns its own asyncio event loop.

The FastAPI event loop never blocks on OPC UA: it calls `pool.submit("conn_a", coro)`
which bridges via `asyncio.run_coroutine_threadsafe` to the target thread's loop
and returns an awaitable future on the API loop. Thread lifetimes are tied to
the process; `start()` blocks until every loop is ready, `stop()` drains
pending tasks and joins every thread.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, TypeVar

T = TypeVar("T")

_logger = logging.getLogger(__name__)


def _quiet_asyncua_shutdown(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
    """Loop exception handler that downgrades the harmless `ConnectionError`
    asyncua's `_monitor_server_loop` raises during shutdown / reconnect.

    asyncua keeps a server-state poll task running in the background; when
    we tear down a client mid-poll (Ctrl+C, reconnect, server gone) the task
    finishes with `ConnectionError("Connection is closed")` and asyncio's
    default handler logs it at ERROR level — pure noise, not a real failure.
    Anything else falls through to the default handler.
    """
    exc = context.get("exception")
    msg = context.get("message", "")
    is_monitor = "_monitor_server_loop" in msg or (
        isinstance(exc, ConnectionError) and "Connection is closed" in str(exc)
    )
    if is_monitor:
        _logger.debug("suppressed asyncua monitor shutdown: %s", msg or exc)
        return
    loop.default_exception_handler(context)


class _Worker:
    __slots__ = ("_ready", "_thread", "loop", "name")

    def __init__(self, name: str) -> None:
        self.name = name
        self.loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    async def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name=f"opcua-{self.name}", daemon=True)
        self._thread.start()
        # Wait (on the API loop) until the worker's loop is accepting calls.
        await asyncio.to_thread(self._ready.wait)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        # Filter the harmless asyncua-monitor shutdown noise; everything
        # else falls through to the default handler.
        loop.set_exception_handler(_quiet_asyncua_shutdown)
        asyncio.set_event_loop(loop)
        self.loop = loop
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

    async def stop(self) -> None:
        loop = self.loop
        thread = self._thread
        if loop is None or thread is None:
            return
        loop.call_soon_threadsafe(loop.stop)
        await asyncio.to_thread(thread.join)
        self._thread = None
        self.loop = None
        self._ready.clear()


class ThreadPool:
    """Workers indexed by name, each running its own asyncio event loop.
    Workers are created lazily on first call. Used in production keyed by
    `connection.name`; tests may use any string."""

    __slots__ = ("_lock", "_workers")

    def __init__(self) -> None:
        self._workers: dict[str, _Worker] = {}
        self._lock = asyncio.Lock()

    @property
    def names(self) -> list[str]:
        return list(self._workers)

    async def _ensure(self, name: str) -> _Worker:
        async with self._lock:
            worker = self._workers.get(name)
            if worker is None:
                worker = _Worker(name)
                self._workers[name] = worker
                await worker.start()
            return worker

    def loop_of(self, name: str) -> asyncio.AbstractEventLoop:
        worker = self._workers.get(name)
        if worker is None or worker.loop is None:
            raise RuntimeError(f"thread {name!r} not started")
        return worker.loop

    async def stop(self) -> None:
        await asyncio.gather(*(w.stop() for w in self._workers.values()))

    async def submit(self, name: str, coro: Coroutine[Any, Any, T]) -> T:
        worker = await self._ensure(name)
        assert worker.loop is not None
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, worker.loop))

    async def run_on(self, name: str, fn: Callable[[], Awaitable[T]]) -> T:
        worker = await self._ensure(name)
        assert worker.loop is not None

        async def _invoke() -> T:
            return await fn()

        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(_invoke(), worker.loop))

    def schedule(self, name: str, coro: Coroutine[Any, Any, Any]) -> None:
        worker = self._workers.get(name)
        if worker is None or worker.loop is None:
            raise RuntimeError(
                f"thread {name!r} not started — schedule() requires a worker that already exists"
            )
        asyncio.run_coroutine_threadsafe(coro, worker.loop)
