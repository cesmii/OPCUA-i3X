"""Hexagonal ports. The only contracts `core` / `api` depend on.

the sole implementor allowed to `import asyncua` is
`i3xua.adapters.asyncua`. Tests substitute a `FakeUpstreamDataSource`
that satisfies this protocol.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from typing import Protocol, runtime_checkable

from i3xua.core.neutral import (
    BrowseResult,
    ConnectionId,
    SubscriptionHandle,
    ValueSample,
)


@runtime_checkable
class UpstreamDataSource(Protocol):
    """Everything the i3X core needs from the OPC UA side."""

    async def list_connections(self) -> list[ConnectionId]: ...

    async def browse(self, connection: ConnectionId) -> BrowseResult:
        """Full snapshot of a connection's exposed namespaces, types, and nodes."""

    async def read_values(
        self, connection: ConnectionId, node_ids: Iterable[str]
    ) -> list[ValueSample]:
        """One-shot read; used by POST /objects/value when no subscription exists."""

    async def add_monitored_items(
        self,
        handle: SubscriptionHandle,
        node_ids: Iterable[str],
        *,
        publishing_ms: int | None = None,
        sampling_ms: int | None = None,
    ) -> None:
        """auto-tier: subscribe each node_id at the requested (publishing,
        sampling) interval pair. None means "use the connection's defaults"
        (`ConnectionConfig.default_*_interval_ms`). Implementations create a new
        asyncua `Subscription` per distinct PublishingInterval on demand."""

    async def remove_monitored_items(
        self,
        handle: SubscriptionHandle,
        node_ids: Iterable[str],
        *,
        publishing_ms: int | None = None,
    ) -> None:
        """tear down the MonitoredItems attached to the (publishing_ms)
        tier. None means "the connection's default tier"."""

    def stream(self, handle: SubscriptionHandle) -> AsyncIterator[ValueSample]:
        """Async iterator yielding samples from a Subscription's fan-out queue."""


@runtime_checkable
class SubscriptionSink(Protocol):
    """Receives samples from the adapter. Implemented by `core.subscriptions`."""

    async def push(self, sample: ValueSample) -> None: ...


@runtime_checkable
class BridgeClock(Protocol):
    """Injectable clock; tests use a FakeClock to drive deterministic sequence numbers."""

    def now_iso(self) -> str: ...
    def monotonic_ns(self) -> int: ...
