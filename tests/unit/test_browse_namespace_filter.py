"""Namespace emission policy — symmetric with native UA NamespaceArray.

Rules:
1. Every URI in the server's NamespaceArray is emitted, in NamespaceArray
   order — the i3X namespace list mirrors what an OPC UA browse client
   would see (the prior "filter to nodes actually walked" policy hid
   server-declared but runtime-empty namespaces, which surprised users
   comparing the two surfaces).
2. The OPC UA core namespace (`http://opcfoundation.org/UA/`, ns=0) is
   emitted UNCONDITIONALLY and FIRST — even if the server omits it from
   NamespaceArray (impossible per spec, but defensive). This preserves
   the `result.namespaces[0].uri == UA core URI` invariant that
   `default_ns` in upstream.py relies on.
3. `namespace_allowlist`, when set, narrows OTHER namespaces but cannot
   drop ns=0.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from i3xua.adapters.asyncua.browse import (
    BrowseConfig,
    BrowsedChild,
    InstanceDeclaration,
    NodeInfo,
    TypeInfo,
    browse,
)
from i3xua.core.neutral import NodeClass

UA_CORE = "http://opcfoundation.org/UA/"


def _suffixed(uri: str, conn: str = "conn_ref") -> str:
    """Per D-34, /v1/namespaces emits each UA namespace with a per-connection
    collision suffix so two servers publishing the same custom URI don't
    collide in the registry."""
    return f"{uri}#connection={conn}"


@dataclass
class FakeSource:
    ns_array: list[str]
    nodes: dict[str, NodeInfo]
    types: dict[str, TypeInfo]
    browse_children_calls: int = 0  # for round-trip-count assertions
    browse_children_call_sizes: list[int] = field(default_factory=list)

    async def get_namespace_array(self) -> list[str]:
        return list(self.ns_array)

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
    children: tuple[tuple[str, str], ...] = (),
) -> NodeInfo:
    name = nid.split("=")[-1]
    return NodeInfo(
        node_id=nid,
        browse_name=name,
        display_name=name,
        node_class=cls,
        namespace_uri=ns,
        data_type_node_id=None,
        type_definition_node_id=None,
        parent_node_id=parent,
        has_component_children=False,
        hierarchical_refs=children,
    )


# ------------------------------------------------------------------ policy tests


async def test_all_advertised_namespaces_are_emitted() -> None:
    """Every URI in the server's NamespaceArray appears in the result —
    even namespaces with zero referenced nodes. The Reference Server, for
    example, declares `…/Data/Instance` and `…/Alarms/Instance` but never
    populates them; a client comparing /v1/namespaces with the OPC UA
    NamespaceArray expects to see them anyway.
    """
    source = FakeSource(
        ns_array=[UA_CORE, "urn:unused", "urn:demo", "urn:ghost"],
        nodes={
            "ns=0;i=85": _node("ns=0;i=85", ns=UA_CORE, children=(("ns=0;i=47", "ns=2;s=Real"),)),
            "ns=2;s=Real": _node("ns=2;s=Real", ns="urn:demo", parent="ns=0;i=85"),
        },
        types={},
    )
    result = await browse(source, connection="conn_ref")
    uris = [ns.uri for ns in result.namespaces]
    assert uris == [
        _suffixed(UA_CORE),
        _suffixed("urn:unused"),
        _suffixed("urn:demo"),
        _suffixed("urn:ghost"),
    ]


async def test_namespace_emission_preserves_ns_array_order() -> None:
    """NamespaceArray order is preserved (ns=0 forced first if not already)."""
    source = FakeSource(
        ns_array=[UA_CORE, "urn:b", "urn:a", "urn:c"],
        nodes={"ns=0;i=85": _node("ns=0;i=85", ns=UA_CORE)},
        types={},
    )
    result = await browse(source, connection="conn_ref")
    assert [ns.uri for ns in result.namespaces] == [
        _suffixed(UA_CORE),
        _suffixed("urn:b"),
        _suffixed("urn:a"),
        _suffixed("urn:c"),
    ]


async def test_ns_zero_emitted_even_when_not_referenced() -> None:
    """ns=0 (UA core) is emitted unconditionally. Even if NO browsed node
    carries `namespace_uri=UA_CORE`, the result MUST still include it.
    This preserves the `result.namespaces[0].uri == UA_CORE` invariant
    that upstream.py's `default_ns` lookup depends on.
    """
    source = FakeSource(
        ns_array=[UA_CORE, "urn:demo"],
        nodes={
            # Root Object is in urn:demo, NOT in ns=0.
            "ns=0;i=85": _node("ns=0;i=85", ns="urn:demo"),
        },
        types={},
    )
    result = await browse(source, connection="conn_ref")
    uris = [ns.uri for ns in result.namespaces]
    assert _suffixed(UA_CORE) in uris, f"ns=0 must always be emitted, got {uris}"


async def test_allowlist_cannot_drop_ns_zero() -> None:
    """`namespace_allowlist` narrows OTHER namespaces but cannot drop
    ns=0. Even when the user's allowlist explicitly excludes ns=0, we
    still emit it — the invariant is a hard guarantee, not a default.
    """
    source = FakeSource(
        ns_array=[UA_CORE, "urn:demo", "urn:other"],
        nodes={
            "ns=0;i=85": _node(
                "ns=0;i=85",
                ns=UA_CORE,
                children=(("ns=0;i=47", "ns=2;s=D"), ("ns=0;i=47", "ns=3;s=O")),
            ),
            "ns=2;s=D": _node("ns=2;s=D", ns="urn:demo", parent="ns=0;i=85"),
            "ns=3;s=O": _node("ns=3;s=O", ns="urn:other", parent="ns=0;i=85"),
        },
        types={},
    )
    # Allowlist does NOT include UA_CORE, only urn:demo.
    cfg = BrowseConfig(namespace_allowlist=("urn:demo",))
    result = await browse(source, connection="conn_ref", cfg=cfg)
    uris = {ns.uri for ns in result.namespaces}
    # urn:other is excluded by allowlist → dropped
    assert _suffixed("urn:other") not in uris
    # urn:demo is in allowlist → kept
    assert _suffixed("urn:demo") in uris
    # ns=0 not in allowlist but invariant → kept anyway
    assert _suffixed(UA_CORE) in uris


async def test_empty_browse_still_emits_ns_zero() -> None:
    """Even with zero browsed nodes or types, ns=0 is emitted so the
    `default_ns = result.namespaces[0].uri` fallback at upstream.py:815
    never crashes on an empty list.
    """
    source = FakeSource(
        ns_array=[UA_CORE],
        nodes={},  # root walk will come up empty
        types={},
    )
    result = await browse(source, connection="conn_ref")
    uris = [ns.uri for ns in result.namespaces]
    assert uris == [_suffixed(UA_CORE)], f"empty browse must emit ns=0, got {uris}"


async def test_ns_zero_is_always_first_in_result() -> None:
    """Invariant — `result.namespaces[0].uri == UA_CORE` always.
    `default_ns` at upstream.py:815 reads `result.namespaces[0].uri` as the
    fallback for type loading; it must always be the UA core URI.
    """
    source = FakeSource(
        # Deliberately put UA_CORE LAST in ns_array — the result must still
        # surface it first.
        ns_array=["urn:demo", "urn:other", UA_CORE],
        nodes={
            "ns=0;i=85": _node("ns=0;i=85", ns="urn:demo", children=(("ns=0;i=47", "ns=3;s=X"),)),
            "ns=3;s=X": _node("ns=3;s=X", ns="urn:other", parent="ns=0;i=85"),
        },
        types={},
    )
    result = await browse(source, connection="conn_ref")
    assert result.namespaces[0].uri == _suffixed(UA_CORE), (
        f"ns=0 must be first, got {[ns.uri for ns in result.namespaces]}"
    )


async def test_advertised_namespaces_emitted_regardless_of_node_or_type_use() -> None:
    """Whether a namespace is referenced by browsed nodes, by TypeDescriptors,
    or by neither, NamespaceArray membership alone qualifies it for emission.
    """
    source = FakeSource(
        ns_array=[UA_CORE, "urn:type-only", "urn:unused"],
        nodes={"ns=0;i=85": _node("ns=0;i=85", ns=UA_CORE)},
        types={
            "ns=0;i=58": TypeInfo(
                node_id="ns=0;i=58",
                display_name="BaseObjectType",
                namespace_uri=UA_CORE,
                fields=(),
                subtypes=("ns=4;s=CustomType",),
            ),
            "ns=4;s=CustomType": TypeInfo(
                node_id="ns=4;s=CustomType",
                display_name="CustomType",
                namespace_uri="urn:type-only",
                fields=(InstanceDeclaration(name="X", data_type_node_id="ns=0;i=11"),),
                subtypes=(),
            ),
        },
    )
    result = await browse(source, connection="conn_ref")
    uris = {ns.uri for ns in result.namespaces}
    assert _suffixed("urn:type-only") in uris
    assert _suffixed("urn:unused") in uris
