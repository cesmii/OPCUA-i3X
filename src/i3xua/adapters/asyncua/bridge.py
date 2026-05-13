"""Thin helpers over `janus.Queue` for adapter<->API loop hand-off.

janus already exposes both a sync side (used from the asyncua thread's loop
via `sync_q.put_nowait`) and an async side (awaited from the FastAPI loop via
`async_q.get`). These helpers add sentinel-based termination so an async
iterator consumer can drain and exit cleanly at shutdown.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import janus

_SENTINEL: Any = object()


def new_queue(maxsize: int = 0) -> janus.Queue[Any]:
    """Queue usable from both a sync producer thread and an async consumer loop."""
    return janus.Queue(maxsize=maxsize)


def close_queue(queue: janus.Queue[Any]) -> None:
    """Signal async consumers to exit their `iter_async` loop."""
    queue.sync_q.put(_SENTINEL)


async def iter_async(queue: janus.Queue[Any]) -> AsyncIterator[Any]:
    """Async generator that yields items until `close_queue` was invoked."""
    while True:
        item = await queue.async_q.get()
        queue.async_q.task_done()
        if item is _SENTINEL:
            return
        yield item
