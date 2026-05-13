"""browse algorithm with a fake BrowseSource."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from i3xua.adapters.asyncua.browse import (
    BrowseConfig,
    BrowsedChild,
    InstanceDeclaration,
    NodeInfo,
    TypeInfo,
    _adaptive_batch_size,
    browse,
)
from i3xua.core.neutral import NodeClass
from i3xua.core.neutral import NodeClass as _NC


@dataclass
class FakeSource:
    ns_array: list[str]
    nodes: dict[str, NodeInfo]
    types: dict[str, TypeInfo]
    browse_children_calls: int = 0  # for round-trip-count assertions
    browse_children_call_sizes: list[int] = field(default_factory=list)
    application_uri: str | None = "urn:test:server"
    application_name: str | None = "Test Server"

    async def get_namespace_array(self) -> list[str]:
        return list(self.ns_array)

    async def get_application_uri(self) -> str | None:
        return self.application_uri

    async def get_application_name(self) -> str | None:
        return self.application_name

    async def get_node_info(self, node_id: str) -> NodeInfo | None:
        return self.nodes.get(node_id)

    async def get_node_info_batch(self, node_ids: list[str]) -> list[NodeInfo | None]:
        return [self.nodes.get(nid) for nid in node_ids]

    async def browse_children(self, parent_node_ids: list[str]) -> list[BrowsedChild]:
        from i3xua.adapters.asyncua.browse import (
            HIERARCHICAL_REFS,
            RELATIONSHIP_NAMES,
        )

        self.browse_children_calls += 1
        self.browse_children_call_sizes.append(len(parent_node_ids))
        out: list[BrowsedChild] = []
        for parent_nid in parent_node_ids:
            parent_info = self.nodes.get(parent_nid)
            if parent_info is None:
                continue
            for ref_type, child_nid in parent_info.hierarchical_refs:
                if ref_type not in HIERARCHICAL_REFS:
                    continue
                child_info = self.nodes.get(child_nid)
                if child_info is None:
                    continue
                out.append(
                    BrowsedChild(
                        info=child_info,
                        parent_node_id=parent_nid,
                        parent_relationship=RELATIONSHIP_NAMES.get(ref_type, ""),
                    )
                )
        return out

    async def get_type_info(self, node_id: str) -> TypeInfo | None:
        return self.types.get(node_id)


def _node(
    nid: str,
    *,
    parent: str | None = None,
    cls: NodeClass = NodeClass.Object,
    ns: str = "urn:demo",
    dt: str | None = None,
    children: tuple[tuple[str, str], ...] = (),
    type_def: str | None = None,
    composition: bool = False,
) -> NodeInfo:
    name = nid.split("=")[-1]
    return NodeInfo(
        node_id=nid,
        browse_name=name,
        display_name=name,
        node_class=cls,
        namespace_uri=ns,
        data_type_node_id=dt,
        type_definition_node_id=type_def,
        parent_node_id=parent,
        has_component_children=composition,
        hierarchical_refs=children,
    )


def _type(
    nid: str,
    *,
    ns: str = "urn:demo",
    fields: tuple[InstanceDeclaration, ...] = (),
    subtypes: tuple[str, ...] = (),
) -> TypeInfo:
    return TypeInfo(
        node_id=nid,
        display_name=nid.split("=")[-1],
        namespace_uri=ns,
        fields=fields,
        subtypes=subtypes,
    )


# ------------------------------------------------------------------ tests


async def test_browse_walks_hierarchical_references() -> None:
    source = FakeSource(
        ns_array=["http://opcfoundation.org/UA/", "urn:demo"],
        nodes={
            "ns=0;i=85": _node(
                "ns=0;i=85",
                ns="http://opcfoundation.org/UA/",
                composition=True,
                children=(("ns=0;i=47", "ns=2;s=Boiler"),),
            ),
            "ns=2;s=Boiler": _node(
                "ns=2;s=Boiler",
                parent="ns=0;i=85",
                composition=True,
                children=(
                    ("ns=0;i=47", "ns=2;s=Boiler/Temperature"),
                    ("ns=0;i=46", "ns=2;s=Boiler/Pressure"),
                ),
            ),
            "ns=2;s=Boiler/Temperature": _node(
                "ns=2;s=Boiler/Temperature",
                parent="ns=2;s=Boiler",
                cls=NodeClass.Variable,
                dt="ns=0;i=11",
            ),
            "ns=2;s=Boiler/Pressure": _node(
                "ns=2;s=Boiler/Pressure",
                parent="ns=2;s=Boiler",
                cls=NodeClass.Variable,
                dt="ns=0;i=11",
            ),
        },
        types={},
    )
    result = await browse(source, connection="conn_ref")
    node_ids = {n.node_id for n in result.nodes}
    assert node_ids == {
        "ns=0;i=85",
        "ns=2;s=Boiler",
        "ns=2;s=Boiler/Temperature",
        "ns=2;s=Boiler/Pressure",
    }
    # Each discovered Variable carries a DataType-sourced type id.
    temp = next(n for n in result.nodes if n.node_id == "ns=2;s=Boiler/Temperature")
    assert temp.type_source_id == "ns=0;i=11"
    assert temp.parent_node_id == "ns=2;s=Boiler"


async def test_non_hierarchical_reference_is_not_followed() -> None:
    source = FakeSource(
        ns_array=["urn:demo"],
        nodes={
            "ns=0;i=85": _node(
                "ns=0;i=85",
                children=(("ns=0;i=40", "ns=2;s=Hidden"),),  # HasTypeDefinition
            ),
            "ns=2;s=Hidden": _node("ns=2;s=Hidden"),
        },
        types={},
    )
    result = await browse(source, connection="conn_ref")
    assert {n.node_id for n in result.nodes} == {"ns=0;i=85"}


async def test_cycle_is_visited_once() -> None:
    source = FakeSource(
        ns_array=["urn:demo"],
        nodes={
            "ns=0;i=85": _node("ns=0;i=85", children=(("ns=0;i=47", "ns=2;s=A"),)),
            "ns=2;s=A": _node("ns=2;s=A", children=(("ns=0;i=47", "ns=2;s=B"),)),
            "ns=2;s=B": _node(
                # Cycle back to A through HasComponent (pathological but legal).
                "ns=2;s=B",
                children=(("ns=0;i=47", "ns=2;s=A"),),
            ),
        },
        types={},
    )
    result = await browse(source, connection="conn_ref")
    ids = [n.node_id for n in result.nodes]
    assert sorted(ids) == sorted({"ns=0;i=85", "ns=2;s=A", "ns=2;s=B"})


async def test_namespace_allowlist_filters_instances_but_keeps_walk() -> None:
    """Allowlist drops `urn:plant:cold` but cannot drop ns=0 — the UA core
    URI is always emitted first to preserve the default_ns invariant.
    """
    source = FakeSource(
        ns_array=["http://opcfoundation.org/UA/", "urn:plant:hot", "urn:plant:cold"],
        nodes={
            "ns=0;i=85": _node(
                "ns=0;i=85",
                ns="http://opcfoundation.org/UA/",
                children=(
                    ("ns=0;i=47", "ns=2;s=HotBoiler"),
                    ("ns=0;i=47", "ns=3;s=ColdTank"),
                ),
            ),
            "ns=2;s=HotBoiler": _node("ns=2;s=HotBoiler", ns="urn:plant:hot", parent="ns=0;i=85"),
            "ns=3;s=ColdTank": _node("ns=3;s=ColdTank", ns="urn:plant:cold", parent="ns=0;i=85"),
        },
        types={},
    )
    cfg = BrowseConfig(namespace_allowlist=("urn:plant:hot",))
    result = await browse(source, connection="conn_ref", cfg=cfg)
    # ns=0 is always emitted (default_ns invariant); urn:plant:hot is kept;
    # urn:plant:cold is filtered out by the allowlist.
    uris = [n.uri for n in result.namespaces]
    assert "http://opcfoundation.org/UA/#connection=conn_ref" in uris
    assert "urn:plant:hot#connection=conn_ref" in uris
    assert "urn:plant:cold#connection=conn_ref" not in uris
    node_ids = {n.node_id for n in result.nodes}
    assert node_ids == {"ns=2;s=HotBoiler"}


async def test_type_walk_builds_structural_hashes_and_schemas() -> None:
    source = FakeSource(
        ns_array=["urn:demo"],
        nodes={"ns=0;i=85": _node("ns=0;i=85")},
        types={
            "ns=0;i=58": _type(
                "ns=0;i=58",
                subtypes=("ns=2;s=BoilerType",),
            ),
            "ns=2;s=BoilerType": _type(
                "ns=2;s=BoilerType",
                fields=(
                    InstanceDeclaration(name="Temp", data_type_node_id="ns=0;i=11"),
                    InstanceDeclaration(
                        name="Alerts",
                        data_type_node_id="ns=0;i=12",
                        value_rank=1,
                        is_optional=True,
                    ),
                ),
            ),
        },
    )
    result = await browse(source, connection="conn_ref")
    by_id = {t.source_node_id: t for t in result.types}
    boiler = by_id["ns=2;s=BoilerType"]
    assert boiler.structural_hash  # non-empty sha256
    schema = boiler.json_schema
    assert schema["properties"]["Temp"]["type"] == "number"
    assert schema["properties"]["Alerts"] == {
        "type": "array",
        "items": {"type": "string"},
    }
    assert schema["required"] == ["Temp"]


async def test_browse_emits_one_namespace_per_ua_namespace_with_indexed_display() -> None:
    """One i3X namespace per OPC UA NamespaceArray entry. displayName
    format: `<UriLabel>-<ServerNoSpaces>-ns<index>(<full-uri>)`."""
    source = FakeSource(
        ns_array=[
            "http://opcfoundation.org/UA/",
            "http://opcfoundation.org/UA/Boiler/",
            "urn:vendor:custom",
        ],
        nodes={"ns=0;i=85": _node("ns=0;i=85")},
        types={},
        application_uri="urn:WIN:KepwareServer",
        application_name="ThingWorx Kepware Server",
    )
    result = await browse(source, connection="kepware")
    assert len(result.namespaces) == 3
    # URIs carry the connection collision suffix (D-34).
    uris = [n.uri for n in result.namespaces]
    assert uris == [
        "http://opcfoundation.org/UA/#connection=kepware",
        "http://opcfoundation.org/UA/Boiler/#connection=kepware",
        "urn:vendor:custom#connection=kepware",
    ]
    # displayNames carry the indexed server/ns= header + decoded URI.
    names = [n.display_name for n in result.namespaces]
    assert names == ["UA-ns0", "UA-Boiler-ns1", "custom-ns2"]
    for ns in result.namespaces:
        assert ns.application_uri == "urn:WIN:KepwareServer"


async def test_browse_namespace_display_falls_back_to_connection_when_app_name_missing() -> None:
    """Without ApplicationName, the displayName falls back to using the
    connection name as the server label."""
    source = FakeSource(
        ns_array=["http://opcfoundation.org/UA/", "urn:demo"],
        nodes={"ns=0;i=85": _node("ns=0;i=85")},
        types={},
        application_uri=None,
        application_name=None,
    )
    result = await browse(source, connection="conn_ref")
    names = [n.display_name for n in result.namespaces]
    assert names[0] == "UA-ns0"
    # urn:demo has no second `:` segment → label falls back to "demo".
    assert names[1] == "demo-ns1"
    for ns in result.namespaces:
        assert ns.application_uri is None


async def test_browse_renames_hierarchy_root_to_application_name() -> None:
    """Hierarchy roots (parent_node_id is None) are renamed to the
    server's ApplicationName so i3X Explorer shows the connected server
    instead of the OPC UA "Objects" folder name."""
    source = FakeSource(
        ns_array=["http://opcfoundation.org/UA/", "urn:demo"],
        nodes={
            "ns=0;i=85": _node(
                "ns=0;i=85",
                ns="http://opcfoundation.org/UA/",
                composition=True,
                children=(("ns=0;i=47", "ns=2;s=Boiler"),),
            ),
            "ns=2;s=Boiler": _node("ns=2;s=Boiler", parent="ns=0;i=85", ns="urn:demo"),
        },
        types={},
        application_uri="urn:WIN:KepwareServer",
        application_name="ThingWorx Kepware Server",
    )
    result = await browse(source, connection="kepware")
    by_id = {n.node_id: n for n in result.nodes}
    # Root: renamed to ApplicationName.
    assert by_id["ns=0;i=85"].display_name == "ThingWorx Kepware Server"
    # Non-root: preserved.
    assert by_id["ns=2;s=Boiler"].display_name == "Boiler"


async def test_browse_stamps_application_uri_on_descriptors() -> None:
    """Types and instances carry the connection's ApplicationUri so the
    mapping layer can compose namespaceUri without per-call lookups."""
    source = FakeSource(
        ns_array=["http://opcfoundation.org/UA/", "urn:demo"],
        nodes={"ns=0;i=85": _node("ns=0;i=85", ns="urn:demo")},
        types={},
        application_uri="urn:WIN:Server",
        application_name="Server",
    )
    result = await browse(source, connection="conn_ref")
    for n in result.nodes:
        assert n.application_uri == "urn:WIN:Server"
    for t in result.types:
        assert t.application_uri == "urn:WIN:Server"


def test_browsed_child_is_frozen_and_slotted() -> None:
    info = NodeInfo(
        node_id="ns=2;s=child",
        browse_name="child",
        display_name="child",
        node_class=_NC.Variable,
        namespace_uri="urn:x",
    )
    child = BrowsedChild(
        info=info,
        parent_node_id="ns=2;s=parent",
        parent_relationship="HasComponent",
    )
    assert child.info is info
    assert child.parent_node_id == "ns=2;s=parent"
    assert child.parent_relationship == "HasComponent"
    import dataclasses

    with pytest.raises(dataclasses.FrozenInstanceError):
        child.info = info  # type: ignore[misc]


async def test_browse_skips_leaves() -> None:
    """Variables and Methods MUST NOT be queued for re-browsing.

    Setup: 1 Object root with 5 Object children, each holding 100
    Variable children. Walker discovers all 506 nodes (1 root + 5 Objects
    + 500 Variables) but only calls browse_children() for the 6 Objects.
    Variables/Methods being non-browseable is what kills the leaf-storm
    against Kepware-shaped flat-tag namespaces.
    """
    nodes: dict[str, NodeInfo] = {}
    nodes["i=85"] = _node(
        "i=85",
        cls=NodeClass.Object,
        children=tuple(("i=47", f"ns=2;s=child{i}") for i in range(5)),
    )
    for i in range(5):
        var_refs = tuple(("i=47", f"ns=2;s=child{i}.var{j}") for j in range(100))
        nodes[f"ns=2;s=child{i}"] = _node(
            f"ns=2;s=child{i}",
            cls=NodeClass.Object,
            ns="urn:demo",
            children=var_refs,
        )
        for j in range(100):
            nodes[f"ns=2;s=child{i}.var{j}"] = _node(
                f"ns=2;s=child{i}.var{j}",
                cls=NodeClass.Variable,
                ns="urn:demo",
                dt="i=11",
            )

    src = FakeSource(ns_array=["urn:demo"], nodes=nodes, types={})
    cfg = BrowseConfig(instance_roots=("i=85",), type_roots=())
    result = await browse(src, connection="test", cfg=cfg)

    discovered_ids = {n.node_id for n in result.nodes}
    # All 506 nodes appear in the result (1 root + 5 Objects + 500 Variables).
    assert len(discovered_ids) == 506
    # browse_children was called for the root and the 5 Object children only.
    # With the default batch_size, the BFS uses 1 call for the root batch
    # and 1 call for the 5-Object batch — so at most 2 calls total.
    # Variables MUST NOT be browsed.
    assert src.browse_children_calls <= 2, (
        f"browse_children was called {src.browse_children_calls} times — "
        f"expected ≤2 (root + 5 Objects). Variables are being queued for "
        f"re-browsing, which would re-introduce the leaf-storm against "
        f"Kepware-shaped namespaces."
    )


# ------------------------------------------------------------------ _adaptive_batch_size


def test_adaptive_batch_size_with_zero_mean() -> None:
    """When running mean is 0 (no parents browsed yet), batch size is
    capped by max_parents_per_batch (after the floor of 1 child/parent
    safety minimum applies)."""
    n = _adaptive_batch_size(
        queue_len=10_000,
        mean_children=0.0,
        max_children=50_000,
        max_parents=1_000,
    )
    assert n == 1_000  # max_parents wins; payload estimate would allow more


def test_adaptive_batch_size_clamped_by_queue_len() -> None:
    """When the queue has fewer parents than the cap, batch is queue_len."""
    n = _adaptive_batch_size(
        queue_len=37,
        mean_children=0.0,
        max_children=50_000,
        max_parents=1_000,
    )
    assert n == 37


def test_adaptive_batch_size_shrinks_with_fat_parents() -> None:
    """When mean is high (e.g. 10,000 children/parent observed), batch
    is constrained by the children-budget. With multiplier=2 default and
    max_children=50_000, planned-per-parent = 20,000 → batch = 2."""
    n = _adaptive_batch_size(
        queue_len=10_000,
        mean_children=10_000.0,
        max_children=50_000,
        max_parents=1_000,
    )
    assert n == 2  # max(1, int(50_000 / (10_000 * 2.0))) == 2


def test_adaptive_batch_size_grows_on_small_parents() -> None:
    """Mean of 5 children/parent → planned 10/parent → batch = 5,000
    by payload, but clamped by max_parents=1,000."""
    n = _adaptive_batch_size(
        queue_len=10_000,
        mean_children=5.0,
        max_children=50_000,
        max_parents=1_000,
    )
    assert n == 1_000  # max_parents cap fires first


def test_adaptive_batch_size_minimum_one() -> None:
    """Even pathologically high mean must produce batch >= 1."""
    n = _adaptive_batch_size(
        queue_len=10_000,
        mean_children=1_000_000.0,
        max_children=50_000,
        max_parents=1_000,
    )
    assert n == 1


async def test_adaptive_walker_grows_on_small_parents() -> None:
    """Fixture: 1000 Object parents each with 5 Object children → after
    a few batches, the running mean is ~5, so the walker packs many more
    parents per batch than the conservative initial size.
    """
    nodes: dict[str, NodeInfo] = {}
    nodes["i=85"] = _node(
        "i=85",
        cls=NodeClass.Object,
        children=tuple(("i=47", f"ns=2;s=p{i}") for i in range(1000)),
    )
    for i in range(1000):
        # Each Object parent has 5 Object children (which are themselves
        # leaves with no further children — keep the queue bounded).
        child_refs = tuple(("i=47", f"ns=2;s=p{i}.c{j}") for j in range(5))
        nodes[f"ns=2;s=p{i}"] = _node(
            f"ns=2;s=p{i}",
            cls=NodeClass.Object,
            ns="urn:demo",
            children=child_refs,
        )
        for j in range(5):
            nodes[f"ns=2;s=p{i}.c{j}"] = _node(
                f"ns=2;s=p{i}.c{j}",
                cls=NodeClass.Variable,
                ns="urn:demo",
                dt="i=11",
            )

    src = FakeSource(ns_array=["urn:demo"], nodes=nodes, types={})
    cfg = BrowseConfig(
        instance_roots=("i=85",),
        type_roots=(),
        max_parents_per_batch=5_000,  # raise cap so payload-budget rules
        max_children_per_batch=50_000,
    )
    result = await browse(src, connection="test", cfg=cfg)

    # Sanity: discovered all 1 + 1000 + 5000 = 6001 nodes.
    assert len(result.nodes) == 6001

    # First batch: mean=0 → estimated 1x2=2 → batch = 50_000/2 = 25_000,
    # capped to max_parents=5_000, then clamped by queue_len=1.
    assert src.browse_children_call_sizes[0] == 1

    # After browsing i=85 (returned 1000 children), mean is 1000.
    # Second batch: 5_000 / (1000*2) = 2.5 → 2. Hmm, that's tiny.
    # Wait — len(children) is the FLAT list across the whole batch.
    # i=85's batch had 1 parent and produced 1000 children, so mean=1000.
    # Recovery happens after next batch when the 1000-children of
    # actual size-5 parents normalize the mean toward 5.
    # Confirm batch sizes grow over time:
    assert src.browse_children_call_sizes[-1] > src.browse_children_call_sizes[1], (
        f"Expected batch sizes to grow once running mean stabilizes around 5; "
        f"got sequence: {src.browse_children_call_sizes}"
    )
    # Confirm at least one late batch is meaningfully large.
    assert max(src.browse_children_call_sizes) >= 100, (
        f"Expected at least one batch ≥ 100 once the small-parent mean "
        f"is established; got max={max(src.browse_children_call_sizes)}"
    )


async def test_adaptive_walker_shrinks_after_fat_parent() -> None:
    """Fixture: 1 Object parent with 10,000 children → after browsing
    it, the mean rises to 10,000, so subsequent batches shrink.
    """
    nodes: dict[str, NodeInfo] = {}
    nodes["i=85"] = _node(
        "i=85",
        cls=NodeClass.Object,
        children=tuple(("i=47", f"ns=2;s=fat.{i}") for i in range(10_000))
        + tuple(("i=47", f"ns=2;s=more{i}") for i in range(50)),
    )
    # 10K Variable leaves (won't be re-browsed)
    for i in range(10_000):
        nodes[f"ns=2;s=fat.{i}"] = _node(
            f"ns=2;s=fat.{i}",
            cls=NodeClass.Variable,
            ns="urn:demo",
            dt="i=11",
        )
    # 50 small Objects with 5 leaf children each
    for i in range(50):
        leaf_refs = tuple(("i=47", f"ns=2;s=more{i}.leaf{j}") for j in range(5))
        nodes[f"ns=2;s=more{i}"] = _node(
            f"ns=2;s=more{i}",
            cls=NodeClass.Object,
            ns="urn:demo",
            children=leaf_refs,
        )
        for j in range(5):
            nodes[f"ns=2;s=more{i}.leaf{j}"] = _node(
                f"ns=2;s=more{i}.leaf{j}",
                cls=NodeClass.Variable,
                ns="urn:demo",
                dt="i=11",
            )

    src = FakeSource(ns_array=["urn:demo"], nodes=nodes, types={})
    cfg = BrowseConfig(
        instance_roots=("i=85",),
        type_roots=(),
        max_parents_per_batch=1_000,
        max_children_per_batch=50_000,
    )
    result = await browse(src, connection="test", cfg=cfg)

    # Sanity: 1 + 10_000 + 50 + 250 = 10_301 nodes
    assert len(result.nodes) == 10_301

    # After i=85 returns 10_050 children, mean ≈ 10_050.
    # max_children/(mean*2) = 50_000/20_100 ≈ 2 → next batch ≤ 2 parents.
    # Batch sizes after batch 0 must therefore be small initially.
    sizes = src.browse_children_call_sizes
    assert sizes[1] <= 5, (
        f"Expected batch immediately after fat parent to be ≤ 5 parents; "
        f"got {sizes[1]}. Full sequence: {sizes}"
    )


async def test_adaptive_walker_respects_max_parents_cap() -> None:
    """Fixture: 5000 parents, each with 0 children. Running mean stays
    near 0, so the helper would compute a huge batch — but max_parents
    must cap it.
    """
    nodes: dict[str, NodeInfo] = {}
    nodes["i=85"] = _node(
        "i=85",
        cls=NodeClass.Object,
        children=tuple(("i=47", f"ns=2;s=leaf{i}") for i in range(5_000)),
    )
    for i in range(5_000):
        nodes[f"ns=2;s=leaf{i}"] = _node(
            f"ns=2;s=leaf{i}",
            cls=NodeClass.Object,
            ns="urn:demo",
            children=(),
        )

    src = FakeSource(ns_array=["urn:demo"], nodes=nodes, types={})
    cfg = BrowseConfig(
        instance_roots=("i=85",),
        type_roots=(),
        max_parents_per_batch=500,
        max_children_per_batch=50_000,
    )
    result = await browse(src, connection="test", cfg=cfg)

    # Sanity: 1 + 5000 nodes
    assert len(result.nodes) == 5_001

    # No batch may exceed max_parents=500.
    for size in src.browse_children_call_sizes:
        assert size <= 500, (
            f"Batch size {size} exceeded max_parents_per_batch=500. "
            f"Full sequence: {src.browse_children_call_sizes}"
        )


async def test_adaptive_walker_first_batch_with_zero_mean() -> None:
    """Batch 1 has empty running-mean state. Walker should pop up to
    max_parents_per_batch (or queue_len, whichever is smaller).
    """
    nodes: dict[str, NodeInfo] = {}
    # Single root so queue_len after bootstrap = 1.
    nodes["i=85"] = _node(
        "i=85",
        cls=NodeClass.Object,
        children=(("i=47", "ns=2;s=child"),),
    )
    nodes["ns=2;s=child"] = _node(
        "ns=2;s=child",
        cls=NodeClass.Variable,
        ns="urn:demo",
    )

    src = FakeSource(ns_array=["urn:demo"], nodes=nodes, types={})
    cfg = BrowseConfig(
        instance_roots=("i=85",),
        type_roots=(),
        max_parents_per_batch=1_000,
        max_children_per_batch=50_000,
    )
    result = await browse(src, connection="test", cfg=cfg)

    # 2 nodes: i=85 + child.
    assert len(result.nodes) == 2
    # First (and only) browse call had 1 parent — the root i=85.
    assert src.browse_children_call_sizes == [1]
