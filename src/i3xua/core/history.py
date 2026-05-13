"""Per-node in-memory history ring."""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

from i3xua.core.neutral import ValueSample


class HistoryStore:
    __slots__ = ("_lock", "_rings", "_size")

    def __init__(self, *, ring_size: int) -> None:
        self._size = ring_size
        self._rings: dict[str, deque[ValueSample]] = {}
        self._lock = asyncio.Lock()

    async def append(self, sample: ValueSample) -> None:
        async with self._lock:
            ring = self._rings.get(sample.element_id)
            if ring is None:
                ring = deque(maxlen=self._size)
                self._rings[sample.element_id] = ring
            ring.append(sample)

    async def read(
        self,
        element_id: str,
        *,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> list[ValueSample]:
        async with self._lock:
            ring = self._rings.get(element_id)
            if ring is None:
                return []
            out: list[ValueSample] = []
            for sample in ring:
                if start_time is not None and sample.timestamp and sample.timestamp < start_time:
                    continue
                if end_time is not None and sample.timestamp and sample.timestamp > end_time:
                    continue
                out.append(sample)
            return out

    async def depths(self) -> dict[str, int]:
        """{elementId: number of samples currently buffered}. Used by the
        /admin/state introspection endpoint."""
        async with self._lock:
            return {eid: len(ring) for eid, ring in self._rings.items()}

    async def latest(self, element_id: str) -> ValueSample | None:
        """return the most recent sample for this element, or None if none
        have been recorded yet. The stale-LKV fallback path in the values route
        uses this when the upstream read fails mid-reconnect."""
        async with self._lock:
            ring = self._rings.get(element_id)
            if not ring:
                return None
            return ring[-1]


__all__ = ["HistoryStore"]

_ = Any
