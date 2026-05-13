"""ThreadPool and janus bridge smoke tests.

Real threads + loops, but scoped tight so the suite stays sub-second.
"""

from __future__ import annotations

import asyncio
import threading

from i3xua.adapters.asyncua.bridge import close_queue, iter_async, new_queue
from i3xua.adapters.asyncua.runtime import ThreadPool


async def test_submit_runs_on_target_thread() -> None:
    pool = ThreadPool()
    try:

        async def whoami() -> str:
            return threading.current_thread().name

        a = await pool.submit("worker_a", whoami())
        b = await pool.submit("worker_b", whoami())
        assert a == "opcua-worker_a"
        assert b == "opcua-worker_b"
        assert a != b
    finally:
        await pool.stop()


async def test_submit_propagates_exceptions() -> None:
    pool = ThreadPool()
    try:

        async def boom() -> None:
            raise RuntimeError("upstream error")

        try:
            await pool.submit("w", boom())
        except RuntimeError as e:
            assert "upstream error" in str(e)
        else:
            raise AssertionError("expected RuntimeError to bubble out")
    finally:
        await pool.stop()


async def test_unknown_thread_raises() -> None:
    pool = ThreadPool()
    try:
        try:
            pool.loop_of("missing")
        except RuntimeError:
            pass
        else:
            raise AssertionError("expected RuntimeError for unknown thread")
    finally:
        await pool.stop()


async def test_janus_queue_round_trip_across_threads() -> None:
    pool = ThreadPool()
    q = new_queue()
    try:

        async def produce() -> None:
            for i in range(3):
                q.sync_q.put(i)
            close_queue(q)

        producer = asyncio.create_task(pool.submit("producer", produce()))

        received: list[int] = []
        async for item in iter_async(q):
            received.append(item)
        await producer

        assert received == [0, 1, 2]
    finally:
        await pool.stop()


async def test_run_on_helper_accepts_zero_arg_async_fn() -> None:
    pool = ThreadPool()
    try:
        result = await pool.run_on("w", lambda: _async_answer(42))
        assert result == 42
    finally:
        await pool.stop()


async def _async_answer(x: int) -> int:
    await asyncio.sleep(0)
    return x


async def test_pool_auto_creates_workers_on_first_submit() -> None:
    pool = ThreadPool()
    try:

        async def name_of_thread() -> str:
            return threading.current_thread().name

        a = await pool.submit("conn_a", name_of_thread())
        b = await pool.submit("conn_b", name_of_thread())
        assert a == "opcua-conn_a"
        assert b == "opcua-conn_b"
        assert set(pool.names) == {"conn_a", "conn_b"}
    finally:
        await pool.stop()
