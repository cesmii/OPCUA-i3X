"""refcounted subscription session."""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import UTC, datetime

from i3xua.adapters.asyncua.subscribe import (
    SubscriptionSession,
    sample_from_ouajson_data_value,
)
from i3xua.core.neutral import Quality, SubscriptionHandle, ValueSample
from i3xua.ouajson import (
    DataValue,
    StatusCode,
    Variant,
    VariantType,
)


@dataclass
class FakeBackend:
    _seq: itertools.count = field(default_factory=lambda: itertools.count(1))
    subscribe_calls: list[list[str]] = field(default_factory=list)
    unsubscribe_calls: list[list[int]] = field(default_factory=list)

    async def subscribe_data_change(self, node_ids: list[str]) -> list[int]:
        self.subscribe_calls.append(list(node_ids))
        return [next(self._seq) for _ in node_ids]

    async def unsubscribe(self, monitored_item_handles: list[int]) -> None:
        self.unsubscribe_calls.append(list(monitored_item_handles))


def _session() -> tuple[SubscriptionSession, FakeBackend, list[ValueSample]]:
    backend = FakeBackend()
    captured: list[ValueSample] = []

    async def sink(sample: ValueSample) -> None:
        captured.append(sample)

    session = SubscriptionSession(
        handle=SubscriptionHandle(connection="conn_ref", subscription_name="sub1"),
        backend=backend,
        sink=sink,
    )
    return session, backend, captured


async def test_first_add_creates_monitored_items_once() -> None:
    session, backend, _ = _session()
    await session.add_monitored_items(["ns=2;s=A", "ns=2;s=B"])
    await session.add_monitored_items(["ns=2;s=A"])
    assert backend.subscribe_calls == [["ns=2;s=A", "ns=2;s=B"]]
    assert session.interest_count("ns=2;s=A") == 2
    assert session.interest_count("ns=2;s=B") == 1


async def test_last_remove_drops_monitored_item() -> None:
    session, backend, _ = _session()
    await session.add_monitored_items(["ns=2;s=A", "ns=2;s=B"])
    await session.remove_monitored_items(["ns=2;s=A"])
    assert backend.unsubscribe_calls == [[1]]  # first handed handle
    assert session.interest_count("ns=2;s=A") == 0
    assert session.interest_count("ns=2;s=B") == 1


async def test_partial_removes_keep_monitored_alive() -> None:
    session, backend, _ = _session()
    await session.add_monitored_items(["ns=2;s=A"])
    await session.add_monitored_items(["ns=2;s=A"])
    await session.remove_monitored_items(["ns=2;s=A"])
    assert backend.unsubscribe_calls == []
    assert session.interest_count("ns=2;s=A") == 1


async def test_dispatch_forwards_to_sink() -> None:
    session, _, captured = _session()
    sample = ValueSample(
        element_id="conn_ref!ns=2;s=A",
        value={"Type": 6, "Body": 7},
        quality=Quality.Good,
        timestamp="2026-04-14T01:02:03Z",
    )
    await session.dispatch(sample)
    assert captured == [sample]


def test_sample_from_data_value_maps_status_to_quality_enum() -> None:
    dv_good = DataValue(
        value=Variant(VariantType.Int32, 1),
        source_timestamp=datetime(2026, 4, 14, 1, 2, 3, tzinfo=UTC),
    )
    sample = sample_from_ouajson_data_value(
        connection="conn_ref", node_id="ns=2;s=A", data_value=dv_good
    )
    assert sample.quality is Quality.Good
    assert sample.element_id == "conn_ref!ns=2;s=A"
    assert sample.timestamp == "2026-04-14T01:02:03Z"
    assert sample.value == {"Type": 6, "Body": 1}


def test_sample_from_data_value_bad_status_becomes_bad_quality() -> None:
    dv_bad = DataValue(
        value=Variant(VariantType.Int32, 0),
        status=StatusCode(code=0x80340000, symbol="Bad_NoData"),
    )
    sample = sample_from_ouajson_data_value(
        connection="conn_ref", node_id="ns=2;s=A", data_value=dv_bad
    )
    assert sample.quality is Quality.Bad


def test_sample_from_data_value_uncertain_status_becomes_goodnodata() -> None:
    dv_unc = DataValue(
        value=Variant(VariantType.Int32, 0),
        status=StatusCode(code=0x40000000, symbol="Uncertain"),
    )
    sample = sample_from_ouajson_data_value(
        connection="conn_ref", node_id="ns=2;s=A", data_value=dv_unc
    )
    assert sample.quality is Quality.GoodNoData
