"""\u2014 stale-LKV fallback during reconnect window.

Unit-level coverage: the route helpers + HistoryStore.latest work together to
produce a sensible payload when `upstream.read_values` can't reach the server.
"""

from __future__ import annotations

import pytest

from i3xua.api.routes.values import (
    _is_empty_failure,
    _read_with_fallback,
    _stale_lkv_fallback,
)
from i3xua.api.state import AppState
from i3xua.core.neutral import ConnectionId, Quality, ValueSample


async def test_history_store_returns_latest_sample(app_state: AppState) -> None:
    latest = await app_state.history.latest("conn_ref!ns=2;s=Boiler1/Temp")
    assert latest is not None
    assert latest.value == {"Type": 11, "Body": 80.0}


async def test_stale_lkv_fallback_overrides_quality_and_timestamp(app_state: AppState) -> None:
    element_id = "conn_ref!ns=2;s=Boiler1/Temp"
    fallback = await _stale_lkv_fallback(app_state, element_id)
    assert fallback is not None
    assert fallback.value == {"Type": 11, "Body": 80.0}  # preserved
    assert fallback.quality is Quality.Bad  # force Bad
    assert fallback.timestamp.endswith("Z")  # current ISO time


async def test_stale_lkv_fallback_returns_none_when_no_history(app_state: AppState) -> None:
    assert await _stale_lkv_fallback(app_state, "conn_ref!ns=99;s=NeverSampled") is None


def test_is_empty_failure_detects_the_disconnected_marker() -> None:
    bad_empty = ValueSample(element_id="x", value=None, quality=Quality.Bad, timestamp="")
    bad_with_value = ValueSample(
        element_id="x",
        value={"Type": 11, "Body": 1.0},
        quality=Quality.Bad,
        timestamp="2026-04-14T00:00:00Z",
    )
    good = ValueSample(
        element_id="x",
        value={"Type": 11, "Body": 1.0},
        quality=Quality.Good,
        timestamp="2026-04-14T00:00:00Z",
    )
    assert _is_empty_failure(bad_empty)
    assert not _is_empty_failure(bad_with_value)
    assert not _is_empty_failure(good)


@pytest.mark.asyncio
async def test_read_with_fallback_swaps_in_lkv_for_empty_bad(app_state: AppState) -> None:
    # The FakeUpstream fixture returns a "GoodNoData" placeholder for nodes it
    # doesn't know, but our production adapter's empty-Bad pattern is what
    # targets. Simulate it by poking a failing upstream function.
    element_id = "conn_ref!ns=2;s=Boiler1/Temp"
    node_id = "ns=2;s=Boiler1/Temp"
    original = app_state.upstream.read_values

    async def failing_read(_conn: ConnectionId, _nodes):  # type: ignore[no-untyped-def]
        return [ValueSample(element_id=element_id, value=None, quality=Quality.Bad, timestamp="")]

    app_state.upstream.read_values = failing_read  # type: ignore[assignment]
    try:
        out = await _read_with_fallback(app_state, "conn_ref", [(element_id, node_id)])
    finally:
        app_state.upstream.read_values = original  # type: ignore[assignment]

    assert len(out) == 1
    assert out[0].quality is Quality.Bad
    assert out[0].value == {"Type": 11, "Body": 80.0}  # swapped in from HistoryStore
    assert out[0].timestamp.endswith("Z")


@pytest.mark.asyncio
async def test_read_with_fallback_handles_whole_call_exception(app_state: AppState) -> None:
    element_id = "conn_ref!ns=2;s=Boiler1/Temp"
    node_id = "ns=2;s=Boiler1/Temp"
    original = app_state.upstream.read_values

    async def raising_read(_conn: ConnectionId, _nodes):  # type: ignore[no-untyped-def]
        raise RuntimeError("connection thread is mid-reconnect")

    app_state.upstream.read_values = raising_read  # type: ignore[assignment]
    try:
        out = await _read_with_fallback(app_state, "conn_ref", [(element_id, node_id)])
    finally:
        app_state.upstream.read_values = original  # type: ignore[assignment]

    assert len(out) == 1
    assert out[0].quality is Quality.Bad
    assert out[0].value == {"Type": 11, "Body": 80.0}
