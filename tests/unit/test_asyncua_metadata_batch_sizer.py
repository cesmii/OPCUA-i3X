"""Adaptive metadata Read sizing (port of barracuda opcua/client BatchSizer).

The metadata Read in `browse_children` used to send every Variable's seven
attributes in ONE Read. Servers with a per-Read operation cap (the .NET
Reference Server) reject that with BadTooManyOperations and the Variables
silently lost their metadata; huge folders on Kepware risk the request timeout.
"""

from __future__ import annotations

from typing import Any

import pytest
from asyncua import ua
from asyncua.ua.uaerrors import BadTooManyOperations

from i3xua.adapters.asyncua.batch_sizer import BatchSizer, read_metadata_adaptive
from i3xua.adapters.asyncua.upstream import (
    _METADATA_ATTRS,
    _AsyncuaBrowseSource,
    _build_metadata_read_items,
)

ATTRS = len(_METADATA_ATTRS)


class _CappedServer:
    """Fake `client.uaclient` whose Read rejects more than `cap` items."""

    def __init__(self, cap: int, fail_after: int | None = None) -> None:
        self.cap = cap
        self.sizes: list[int] = []
        self.fail_after = fail_after

    async def read(self, params: Any) -> list[Any]:
        n = len(params.NodesToRead)
        self.sizes.append(n)
        if self.fail_after is not None and len(self.sizes) > self.fail_after:
            raise ua.UaError("Failed to send request to OPC UA server")
        if n > self.cap:
            raise ua.UaStatusCodeError(ua.StatusCodes.BadTooManyOperations)
        return [
            ua.DataValue(Value=ua.Variant(Value=-1, VariantType=ua.VariantType.Int32))
            for _ in range(n)
        ]


class _Client:
    def __init__(self, uaclient: _CappedServer) -> None:
        self.uaclient = uaclient


def _items(n_vars: int) -> list[ua.ReadValueId]:
    return _build_metadata_read_items([ua.NodeId(i, 2) for i in range(n_vars)])


def test_sizer_slow_start_then_aimd_and_sticky_cap() -> None:
    s = BatchSizer(ceiling=10_000, seed_window=100, seed_ssthresh=400)
    assert s.next() == 100
    s.on_success(100, 0.0)
    s.on_success(200, 0.0)
    assert s.window == 400  # doubled to ssthresh
    s.on_success(400, 0.0)
    assert s.window == 900  # +500 AIMD
    s.on_rejection(900)
    assert s.hard_cap == 899
    assert s.window == s.ssthresh == 450
    for _ in range(5):
        s.on_success(450, 0.0)
    assert s.next() <= 899  # never above the confirmed cap


def test_ceiling_beats_floor() -> None:
    s = BatchSizer(ceiling=10)
    assert s.next() == 10


def test_latency_caps_window() -> None:
    s = BatchSizer(ceiling=100_000, seed_window=5_000, target_latency_ms=1000)
    s.on_success(1_000, 2.0)  # 2 ms/item → 500 items per second budget
    assert s.next() == 500


def test_sizer_ignores_rejection_at_floor() -> None:
    s = BatchSizer(ceiling=1_000, floor=100)
    s.on_rejection(100)
    assert s.hard_cap == 0


@pytest.mark.asyncio
async def test_adaptive_read_halves_on_unit_boundary() -> None:
    server = _CappedServer(cap=3 * ATTRS)
    items = _items(10)
    dvs, fb = await read_metadata_adaptive(
        lambda chunk: server.read(ua.ReadParameters(NodesToRead=list(chunk))), items, ATTRS
    )
    assert len(dvs) == len(items)
    assert fb.rejected_sizes[0] == 10
    assert all(n % ATTRS == 0 for n in server.sizes)
    assert sum(units for units, _ in fb.accepted) == 10


@pytest.mark.asyncio
async def test_browse_source_reads_everything_under_undeclared_cap() -> None:
    server = _CappedServer(cap=2500)  # .NET Reference Server-ish
    source = _AsyncuaBrowseSource(_Client(server))  # type: ignore[arg-type]
    n_vars = 1000  # 7000 items: one unchunked Read is rejected
    results = await source._read_metadata_chunked(_items(n_vars), n_vars)

    assert len(results) == n_vars * ATTRS
    assert all(r.StatusCode.is_good() for r in results)
    assert source.counters.metadata_read_failures == 0
    # Every rejection is recovered by halving; the sizer's sticky cap only
    # ratchets down by one per rejection (same as barracuda), so a few
    # repeat rejections are expected, but never one per call.
    assert source.counters.metadata_read_rejections < len(server.sizes)
    assert source.sizer.hard_cap > 0


@pytest.mark.asyncio
async def test_browse_source_honours_published_max_nodes_per_read() -> None:
    server = _CappedServer(cap=700)
    source = _AsyncuaBrowseSource(_Client(server), max_nodes_per_read=700)  # type: ignore[arg-type]
    n_vars = 1000
    results = await source._read_metadata_chunked(_items(n_vars), n_vars)
    assert len(results) == n_vars * ATTRS
    assert max(server.sizes) <= 700
    assert source.counters.metadata_read_rejections == 0


@pytest.mark.asyncio
async def test_transport_failure_keeps_alignment_and_counts() -> None:
    server = _CappedServer(cap=10**9, fail_after=1)
    source = _AsyncuaBrowseSource(_Client(server), max_nodes_per_read=100 * ATTRS)  # type: ignore[arg-type]
    n_vars = 250
    results = await source._read_metadata_chunked(_items(n_vars), n_vars)
    assert len(results) == n_vars * ATTRS
    assert all(r.StatusCode.is_good() for r in results[: 100 * ATTRS])
    assert not any(r.StatusCode.is_good() for r in results[100 * ATTRS :])
    assert source.counters.metadata_read_failures == 150


def test_bad_too_many_operations_is_typed() -> None:
    assert isinstance(
        ua.UaStatusCodeError(ua.StatusCodes.BadTooManyOperations), BadTooManyOperations
    )
