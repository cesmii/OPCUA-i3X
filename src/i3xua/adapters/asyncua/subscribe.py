"""Subscription lifecycle with refcounted MonitoredItems.

One `SubscriptionSession` maps to one asyncua `Subscription`. It tracks how
many i3X subscriptions have registered for each OPC UA NodeId; a MonitoredItem
is created on first add and destroyed on last remove. `handle_datachange`
converts an asyncua `DataValue` to a Part-6-encoded `ValueSample` and pushes
it onto the session's sink.

The sink is typically the per-subscription ring-buffer append in
`core.subscriptions`. Plumbing (janus.Queue between the asyncua thread and the
worker thread) is provided by the calling adapter code.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

from i3xua.core.neutral import (
    ElementRef,
    Quality,
    SubscriptionHandle,
    ValueSample,
)
from i3xua.ouajson import DataValue as OUaDataValue
from i3xua.ouajson import encode_data_value


class SubscriptionBackend(Protocol):
    """Minimum asyncua surface the session needs."""

    async def subscribe_data_change(self, node_ids: list[str]) -> list[int]: ...

    async def unsubscribe(self, monitored_item_handles: list[int]) -> None: ...


Sink = Callable[[ValueSample], Awaitable[None]]
"""Async sink invoked for every incoming sample (post-encode)."""


@dataclass
class SubscriptionSession:
    handle: SubscriptionHandle
    backend: SubscriptionBackend
    sink: Sink
    # node_id -> count of i3X subscriptions interested in it
    _refcount: dict[str, int] = field(default_factory=dict)
    # node_id -> asyncua MonitoredItem server-side handle
    _monitored: dict[str, int] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def add_monitored_items(self, node_ids: Iterable[str]) -> None:
        ids = list(node_ids)
        async with self._lock:
            first_time = [nid for nid in ids if self._refcount.get(nid, 0) == 0]
            if first_time:
                handles = await self.backend.subscribe_data_change(first_time)
                for nid, h in zip(first_time, handles, strict=True):
                    self._monitored[nid] = h
            for nid in ids:
                self._refcount[nid] = self._refcount.get(nid, 0) + 1

    async def remove_monitored_items(self, node_ids: Iterable[str]) -> None:
        ids = list(node_ids)
        to_drop: list[int] = []
        async with self._lock:
            for nid in ids:
                count = self._refcount.get(nid, 0)
                if count <= 0:
                    continue
                count -= 1
                if count == 0:
                    self._refcount.pop(nid, None)
                    mh = self._monitored.pop(nid, None)
                    if mh is not None:
                        to_drop.append(mh)
                else:
                    self._refcount[nid] = count
            if to_drop:
                await self.backend.unsubscribe(to_drop)

    def interest_count(self, node_id: str) -> int:
        return self._refcount.get(node_id, 0)

    def monitored_node_ids(self) -> list[str]:
        return list(self._monitored)

    async def dispatch(self, sample: ValueSample) -> None:
        await self.sink(sample)


# ------------------------------------------------------------------ encoding helpers


def sample_from_ouajson_data_value(
    *,
    connection: str,
    node_id: str,
    data_value: OUaDataValue,
) -> ValueSample:
    """Produce a Part-6-encoded ValueSample from an already-neutral DataValue.

    Callers in `adapters.asyncua.<production>` are responsible for translating
    the real `asyncua.ua.DataValue` into `ouajson.DataValue` before invoking
    this; keeping that conversion outside this module means the test suite
    never touches asyncua types.
    """
    encoded = encode_data_value(data_value)
    status = data_value.status.symbol
    if status == "Good":
        quality = Quality.Good
    elif status.startswith("Bad"):
        quality = Quality.Bad
    else:
        quality = Quality.GoodNoData
    return ValueSample(
        element_id=ElementRef(connection, node_id).as_id(),
        value=encoded.get("Value", {}),
        quality=quality,
        timestamp=encoded.get("SourceTimestamp") or encoded.get("ServerTimestamp") or "",
    )


__all__ = [
    "Sink",
    "SubscriptionBackend",
    "SubscriptionSession",
    "sample_from_ouajson_data_value",
]

_ = Any
