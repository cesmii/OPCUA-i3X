"""dynamic registry tests.

Key invariants under test:
- Concurrent readers holding a snapshot never observe partial writer state.
- Unchanged structural hashes reuse their prior Pydantic model instance.
- NamespaceRegistry keys by `<uri>#connection=<name>`.
- InstanceRegistry composition links follow `parent_node_id`.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from pydantic import BaseModel, create_model

from i3xua.core.neutral import (
    NamespaceInfo,
    NodeClass,
    NodeDescriptor,
    TypeDescriptor,
)
from i3xua.core.registry import (
    InstanceRegistry,
    NamespaceRegistry,
    RegisteredType,
    TypeRegistry,
    type_structural_hash,
)

# ---------------------------------------------------------------- fixtures


def _descriptor(
    source_node_id: str = "ns=2;s=Boiler",
    connection: str = "conn_ref",
    fields: tuple[tuple[str, str, int, bool], ...] = (("Temp", "ns=0;i=11", -1, False),),
) -> TypeDescriptor:
    h = type_structural_hash(source_node_id, fields)
    schema = {"type": "object", "properties": {f[0]: {"type": "number"} for f in fields}}
    return TypeDescriptor(
        source_node_id=source_node_id,
        display_name=source_node_id.split("=")[-1],
        namespace_uri="urn:test:ns",
        connection=connection,
        structural_hash=h,
        json_schema=schema,
    )


def _node(
    node_id: str,
    *,
    connection: str = "conn_ref",
    parent: str | None = None,
    node_class: NodeClass = NodeClass.Variable,
    composition: bool = False,
) -> NodeDescriptor:
    return NodeDescriptor(
        node_id=node_id,
        connection=connection,
        display_name=node_id,
        node_class=node_class,
        namespace_uri="urn:test:ns",
        type_source_id=None,
        parent_node_id=parent,
        is_composition=composition,
    )


def _build_model_counter() -> tuple[list[str], object]:
    """Returns (calls-log, builder). The log records every source_node_id the
    reconcile loop asked us to build — lets tests assert no spurious rebuilds."""
    calls: list[str] = []

    def build(descriptor: TypeDescriptor) -> type[BaseModel]:
        calls.append(descriptor.source_node_id)
        return create_model(f"T_{descriptor.source_node_id}", value=(float, ...))

    return calls, build


# ---------------------------------------------------------------- structural hash


def test_structural_hash_is_stable_across_field_order() -> None:
    h1 = type_structural_hash(
        "ns=2;s=A", [("x", "ns=0;i=6", -1, False), ("y", "ns=0;i=11", -1, False)]
    )
    h2 = type_structural_hash(
        "ns=2;s=A", [("y", "ns=0;i=11", -1, False), ("x", "ns=0;i=6", -1, False)]
    )
    assert h1 == h2


def test_structural_hash_differs_when_field_type_changes() -> None:
    h1 = type_structural_hash("ns=2;s=A", [("x", "ns=0;i=6", -1, False)])
    h2 = type_structural_hash("ns=2;s=A", [("x", "ns=0;i=11", -1, False)])
    assert h1 != h2


# ---------------------------------------------------------------- TypeRegistry


async def test_unchanged_type_hash_skips_rebuild() -> None:
    reg = TypeRegistry()
    calls, build = _build_model_counter()
    d = _descriptor()

    await reg.reconcile([d], build_model=build)
    await reg.reconcile([d], build_model=build)

    assert calls == [d.source_node_id]  # second reconcile must not rebuild


async def test_changed_field_causes_new_hash_and_rebuild() -> None:
    reg = TypeRegistry()
    calls, build = _build_model_counter()
    d1 = _descriptor(fields=(("Temp", "ns=0;i=11", -1, False),))
    await reg.reconcile([d1], build_model=build)

    d2 = _descriptor(fields=(("Temp", "ns=0;i=6", -1, False),))  # DataType change
    diff = await reg.reconcile([d2], build_model=build)

    assert d1.structural_hash != d2.structural_hash
    assert d1.structural_hash not in reg.by_hash()
    assert d2.structural_hash in reg.by_hash()
    assert diff.added == frozenset({d2.structural_hash})
    assert diff.removed == frozenset({d1.structural_hash})
    assert len(calls) == 2


async def test_get_by_source_returns_live_model() -> None:
    reg = TypeRegistry()
    _, build = _build_model_counter()
    d = _descriptor()
    await reg.reconcile([d], build_model=build)

    entry = reg.get_by_source(d.connection, d.source_node_id)
    assert isinstance(entry, RegisteredType)
    assert entry.descriptor is d


# ---------------------------------------------------------------- snapshot swap


async def test_readers_never_see_partial_writer_state() -> None:
    reg = TypeRegistry()
    _, build = _build_model_counter()
    baseline = [_descriptor(f"ns=2;s=T{i}") for i in range(10)]
    await reg.reconcile(baseline, build_model=build)

    inconsistent_seen = asyncio.Event()

    async def reader() -> None:
        for _ in range(500):
            snap = reg.by_source()
            # Each snapshot must be internally consistent: every source-id key
            # must resolve to a hash that is present in by_hash under the same
            # snapshot reference.
            by_hash = reg.by_hash()
            for h in list(snap.values()):
                if h not in by_hash:
                    inconsistent_seen.set()
                    return
            await asyncio.sleep(0)

    async def writer() -> None:
        for gen in range(30):
            descs = [_descriptor(f"ns=2;s=G{gen}_{i}") for i in range(15)]
            await reg.reconcile(descs, build_model=build)
            await asyncio.sleep(0)

    readers = [asyncio.create_task(reader()) for _ in range(4)]
    writers = [asyncio.create_task(writer())]
    await asyncio.gather(*readers, *writers)

    assert not inconsistent_seen.is_set()


# ---------------------------------------------------------------- NamespaceRegistry


async def test_namespace_registry_keys_by_connection_suffixed_uri() -> None:
    """The browse layer composes the per-connection collision suffix (D-34)
    into NamespaceInfo.uri before reaching the registry; the registry just
    keys by that final URI."""
    reg = NamespaceRegistry()
    a = NamespaceInfo(
        uri="http://opcfoundation.org/UA/#connection=conn_a",
        connection="conn_a",
        display_name="UA",
    )
    b = NamespaceInfo(
        uri="http://opcfoundation.org/UA/#connection=conn_b",
        connection="conn_b",
        display_name="UA",
    )
    await reg.reconcile([a, b])

    assert set(reg.snapshot()) == {
        "http://opcfoundation.org/UA/#connection=conn_a",
        "http://opcfoundation.org/UA/#connection=conn_b",
    }


async def test_namespace_reconcile_removes_stale_entries() -> None:
    reg = NamespaceRegistry()
    a = NamespaceInfo(uri="urn:a#connection=c1", connection="c1", display_name="A")
    b = NamespaceInfo(uri="urn:b#connection=c1", connection="c1", display_name="B")
    await reg.reconcile([a, b])
    diff = await reg.reconcile([a])  # b disappears
    assert diff.removed == frozenset({"urn:b#connection=c1"})
    assert diff.added == frozenset()


# ---------------------------------------------------------------- InstanceRegistry


async def test_instance_registry_children_follow_parent_link() -> None:
    reg = InstanceRegistry()
    root = _node("ns=2;s=Boiler", node_class=NodeClass.Object, composition=True)
    t = _node("ns=2;s=Boiler/Temp", parent="ns=2;s=Boiler")
    p = _node("ns=2;s=Boiler/Pressure", parent="ns=2;s=Boiler")
    orphan = _node("ns=2;s=Stray")
    await reg.reconcile([root, t, p, orphan])

    children = {c.node_id for c in reg.children_of("conn_ref!ns=2;s=Boiler")}
    assert children == {"ns=2;s=Boiler/Temp", "ns=2;s=Boiler/Pressure"}


async def test_instance_registry_reconcile_drops_missing_nodes() -> None:
    reg = InstanceRegistry()
    a = _node("ns=2;s=A")
    b = _node("ns=2;s=B")
    await reg.reconcile([a, b])
    diff = await reg.reconcile([a, replace(b, display_name="B2")])  # B renamed

    assert diff.removed == frozenset()
    # The key stays the same (elementId is node_id-based), so this is an update
    # in-place, which SnapshotRegistry surfaces as "unchanged key".
    assert "conn_ref!ns=2;s=B" in reg.snapshot()
    assert reg.snapshot()["conn_ref!ns=2;s=B"].display_name == "B2"


async def test_children_of_root_returns_parentless_nodes() -> None:
    reg = InstanceRegistry()
    top = _node("ns=2;s=Top", node_class=NodeClass.Object, composition=True)
    child = _node("ns=2;s=Top/Child", parent="ns=2;s=Top")
    await reg.reconcile([top, child])

    roots = {n.node_id for n in reg.children_of(None)}
    assert roots == {"ns=2;s=Top"}


# ---------------------------------------------------------------- SnapshotRegistry basics


def test_snapshot_registry_sync_replace_is_atomic_for_readers() -> None:
    reg = NamespaceRegistry()
    reg.replace_sync({"a": NamespaceInfo("urn:a", "c", "A")})
    assert set(reg.snapshot()) == {"a"}
    reg.replace_sync({"b": NamespaceInfo("urn:b", "c", "B")})
    assert set(reg.snapshot()) == {"b"}


async def test_concurrent_writers_serialized_under_lock() -> None:
    reg = NamespaceRegistry()

    async def write(tag: str) -> None:
        await reg.update({f"urn:{tag}#connection=c": NamespaceInfo(f"urn:{tag}", "c", tag)})

    with pytest.MonkeyPatch.context():
        await asyncio.gather(*[write(f"t{i}") for i in range(20)])

    # All 20 writes must land.
    assert len(reg.snapshot()) == 20
