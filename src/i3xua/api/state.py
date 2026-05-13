"""AppState — the container stashed on `app.state` for route dependencies."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from i3xua.core.history import HistoryStore
from i3xua.core.registry import (
    InstanceRegistry,
    NamespaceRegistry,
    TypeRegistry,
)
from i3xua.core.subscriptions import SubscriptionManager
from i3xua.ports import UpstreamDataSource
from i3xua.settings import AppConfig


@dataclass(slots=True)
class AppState:
    config: AppConfig
    namespaces: NamespaceRegistry
    types: TypeRegistry
    instances: InstanceRegistry
    subscriptions: SubscriptionManager
    history: HistoryStore
    upstream: UpstreamDataSource
    # Per-connection browse-phase timings populated by the adapter on each
    # successful (re)connect. Read-only from the API side; surfaced via
    # `/admin/state` so the andon report can plot startup performance.
    # Shape: {connection_name: {"started_at", "completed_at", "browse_s",
    # "types_s", "snapshot_s", "total_s", "namespaces", "types",
    # "instances"}}.
    browse_metrics: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Single-process lock around the andon-report regenerate subprocess.
    # /admin/andon/regenerate acquires this without blocking; if held,
    # the route returns 409 Conflict.
    andon_regen_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def build_state(config: AppConfig, upstream: UpstreamDataSource) -> AppState:
    return AppState(
        config=config,
        namespaces=NamespaceRegistry(),
        types=TypeRegistry(),
        instances=InstanceRegistry(),
        subscriptions=SubscriptionManager(ring_size=config.registry.subscription_ring_size),
        history=HistoryStore(ring_size=config.registry.history_ring_size),
        upstream=upstream,
    )


__all__ = ["AppState", "build_state"]
