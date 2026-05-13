"""i3X subscription manager: ring buffers, sequence numbers, fan-out.

Each i3X subscription owns a bounded ring of `SyncResponseItem`s. When an
adapter sample lands, the manager looks up every subscription registered for
the sample's `elementId` and appends the sample (with a monotonic sequence
number) to each matching ring. Clients drain via `sync()` with their last seen
`sequenceNumber`; if the ring dropped samples in the meantime, `dropped=True`
rides along on the response.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from i3xua.core.mapping import _strip_variant
from i3xua.core.neutral import ValueSample
from i3xua.i3x.types import SyncResponseItem


def _new_subscription_id() -> str:
    # Small, URL-friendly, collision-resistant. i3X clients treat it as opaque.
    return f"sub_{secrets.token_urlsafe(9)}"


# Sentinel pushed to stream queues when a subscription is deleted so consumers
# can exit their loop cleanly. `None` is used because we store real items by value.
_STREAM_END: SyncResponseItem | None = None


@dataclass
class _Subscription:
    id: str
    ring_size: int
    ring: deque[SyncResponseItem] = field(default_factory=lambda: deque())
    elements: set[str] = field(default_factory=set)
    next_seq: int = 1
    # Tracks whether the ring has evicted at least one sample since the client's
    # most recent sync; cleared on every successful sync.
    dropped: bool = False
    streamers: list[asyncio.Queue[SyncResponseItem | None]] = field(default_factory=list)


@dataclass
class SyncResult:
    items: list[SyncResponseItem]
    dropped: bool


class SubscriptionManager:
    __slots__ = ("_lock", "_ring_size", "_subs")

    def __init__(self, *, ring_size: int) -> None:
        self._ring_size = ring_size
        self._subs: dict[str, _Subscription] = {}
        self._lock = asyncio.Lock()

    async def create(self) -> str:
        async with self._lock:
            sub = _Subscription(id=_new_subscription_id(), ring_size=self._ring_size)
            sub.ring = deque(maxlen=self._ring_size)
            self._subs[sub.id] = sub
            return sub.id

    async def delete(self, subscription_id: str) -> set[str]:
        """Remove a subscription; returns the set of elementIds that were registered so
        the caller can update adapter-level refcounts."""
        async with self._lock:
            sub = self._subs.pop(subscription_id, None)
            if sub is None:
                return set()
            # Wake any SSE listeners so they can exit their loops.
            for q in sub.streamers:
                with contextlib.suppress(asyncio.QueueFull):
                    q.put_nowait(_STREAM_END)
            return set(sub.elements)

    async def attach_stream(
        self, subscription_id: str, *, maxsize: int = 10_000
    ) -> asyncio.Queue[SyncResponseItem | None]:
        async with self._lock:
            sub = self._require(subscription_id)
            q: asyncio.Queue[SyncResponseItem | None] = asyncio.Queue(maxsize=maxsize)
            sub.streamers.append(q)
            return q

    async def detach_stream(
        self, subscription_id: str, queue: asyncio.Queue[SyncResponseItem | None]
    ) -> None:
        async with self._lock:
            sub = self._subs.get(subscription_id)
            if sub is None:
                return
            with contextlib.suppress(ValueError):
                sub.streamers.remove(queue)

    async def register(
        self, subscription_id: str, element_ids: list[str]
    ) -> tuple[list[str], list[str]]:
        """Add elements to the sub. Returns (all_current, newly_added) for adapter refcount."""
        async with self._lock:
            sub = self._require(subscription_id)
            newly = [eid for eid in element_ids if eid not in sub.elements]
            sub.elements.update(element_ids)
            return sorted(sub.elements), newly

    async def unregister(self, subscription_id: str, element_ids: list[str]) -> list[str]:
        """Drop elements from the sub. Returns the elementIds that were actually removed."""
        async with self._lock:
            sub = self._require(subscription_id)
            removed = [eid for eid in element_ids if eid in sub.elements]
            sub.elements.difference_update(element_ids)
            return removed

    async def list_ids(self) -> list[str]:
        async with self._lock:
            return list(self._subs)

    async def get_elements(self, subscription_id: str) -> set[str]:
        async with self._lock:
            return set(self._require(subscription_id).elements)

    async def summaries(self) -> list[dict[str, Any]]:
        """One dict per subscription with operator-relevant counters. Used by
        /admin/state to render the subscriptions panel."""
        async with self._lock:
            return [
                {
                    "id": sub.id,
                    "element_count": len(sub.elements),
                    "ring_depth": len(sub.ring),
                    "next_sequence": sub.next_seq,
                    "streamers": len(sub.streamers),
                    "dropped": sub.dropped,
                }
                for sub in self._subs.values()
            ]

    async def push(self, sample: ValueSample) -> None:
        """Fan-out: deliver sample to every sub registered for its elementId."""
        async with self._lock:
            for sub in self._subs.values():
                if sample.element_id in sub.elements:
                    self._append_locked(sub, sample)

    async def push_to(self, subscription_id: str, sample: ValueSample) -> None:
        """Deliver to one specific subscription (used by tests + targeted replay)."""
        async with self._lock:
            sub = self._require(subscription_id)
            self._append_locked(sub, sample)

    async def sync(self, subscription_id: str, *, last_sequence_number: int | None) -> SyncResult:
        async with self._lock:
            sub = self._require(subscription_id)
            if last_sequence_number is None:
                items = list(sub.ring)
            else:
                items = [i for i in sub.ring if (i.sequenceNumber or 0) > last_sequence_number]
            dropped = sub.dropped
            sub.dropped = False
            return SyncResult(items=items, dropped=dropped)

    # ------------------------------------------------------------------ internals

    def _require(self, subscription_id: str) -> _Subscription:
        sub = self._subs.get(subscription_id)
        if sub is None:
            raise KeyError(f"unknown subscription: {subscription_id!r}")
        return sub

    def _append_locked(self, sub: _Subscription, sample: ValueSample) -> None:
        item = SyncResponseItem(
            elementId=sample.element_id,
            value=_strip_variant(sample.value),
            timestamp=sample.timestamp or "",
            quality=sample.quality.value,
            sequenceNumber=sub.next_seq,
        )
        sub.next_seq += 1
        if len(sub.ring) == sub.ring_size:
            sub.dropped = True
        sub.ring.append(item)
        # Fan out to every attached SSE listener. Silently drop if a listener
        # is too slow — they'll recover via /subscriptions/sync using the ring.
        for q in sub.streamers:
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(item)


__all__ = ["SubscriptionManager", "SyncResult"]

_ = Any
