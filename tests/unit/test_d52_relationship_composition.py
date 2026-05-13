"""Relationship-aware composition, HasProperty filter, UnknownType.

Covers:
  (a) BFS stamps `parent_relationship` per the ref type that led a child in.
  (b) Post-walk sweep narrows `is_composition` to (Object AND HasComponent).
  (c) `InstanceRegistry.children_of(eid, relationship=...)` filters correctly.
  (d) `to_object_instance` emits `UnknownType` placeholder when `type_source_id=None`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx
import pytest

from i3xua.adapters.asyncua.browse import (
    BrowseConfig,
    BrowsedChild,
    NodeInfo,
    browse,
)
from i3xua.api.state import AppState
from i3xua.core.mapping import to_object_instance
from i3xua.core.neutral import (
    NodeClass,
    NodeDescriptor,
)
from i3xua.core.registry import InstanceRegistry


@dataclass
class _FakeSource:
    ns_array: list[str]
    nodes: dict[str, NodeInfo]
    types: dict[str, object]
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

    async def get_type_info(self, node_id: str):
        return self.types.get(node_id)


def _node(
    nid: str,
    *,
    cls: NodeClass = NodeClass.Object,
    ns: str = "urn:demo",
    children: tuple[tuple[str, str], ...] = (),
) -> NodeInfo:
    return NodeInfo(
        node_id=nid,
        browse_name=nid.split("=")[-1],
        display_name=nid.split("=")[-1],
        node_class=cls,
        namespace_uri=ns,
        hierarchical_refs=children,
        has_component_children=any(r[0] in {"i=47", "ns=0;i=47"} for r in children),
    )


# ------------------------------------------------------------------ (a), (b)


async def test_hasproperty_walk_does_not_mark_parent_as_composition() -> None:
    """A TwoStateVariable (Variable) with a HasProperty child `Id` must NOT
    be flagged composition (RFC §3.2.3 scopes composition to HasComponent on
    Objects). `Id` itself is reached, gets `parent_relationship="HasProperty"`,
    and its parent `AckedState` stays `is_composition=False`.

    With browse_variable_properties=True the walker descends into Variables so
    HasProperty children appear in walk output.
    """
    source = _FakeSource(
        ns_array=["urn:demo"],
        nodes={
            "ns=0;i=85": _node(
                "ns=0;i=85",
                cls=NodeClass.Object,
                children=(("i=47", "ns=2;s=Boiler"),),
            ),
            "ns=2;s=Boiler": _node(
                "ns=2;s=Boiler",
                cls=NodeClass.Object,
                children=(("i=47", "ns=2;s=Boiler/AckedState"),),
            ),
            "ns=2;s=Boiler/AckedState": _node(
                "ns=2;s=Boiler/AckedState",
                cls=NodeClass.Variable,
                children=(("i=46", "ns=2;s=Boiler/AckedState/Id"),),
            ),
            "ns=2;s=Boiler/AckedState/Id": _node(
                "ns=2;s=Boiler/AckedState/Id", cls=NodeClass.Variable
            ),
        },
        types={},
    )
    cfg = BrowseConfig(browse_variable_properties=True)
    result = await browse(source, connection="conn_ref", cfg=cfg)
    by_id = {n.node_id: n for n in result.nodes}

    boiler = by_id["ns=2;s=Boiler"]
    acked = by_id["ns=2;s=Boiler/AckedState"]
    acked_id = by_id["ns=2;s=Boiler/AckedState/Id"]

    # Boiler has a HasComponent child (AckedState) AND is an Object → composition.
    assert boiler.is_composition is True
    assert acked.parent_relationship == "HasComponent"

    # AckedState is a Variable; even with HasProperty child it must NOT be composition.
    assert acked.is_composition is False
    assert acked_id.parent_relationship == "HasProperty"

    # Leaf stays non-composition.
    assert acked_id.is_composition is False


async def test_method_with_inputarguments_stays_non_composition() -> None:
    """Methods are never composition per CESMII §3.2.3 even when they have
    HasProperty children (InputArguments, OutputArguments).

    With browse_variable_properties=True the walker descends into Methods so
    HasProperty children appear in walk output, but the Method stays non-composition.
    """
    source = _FakeSource(
        ns_array=["urn:demo"],
        nodes={
            "ns=0;i=85": _node(
                "ns=0;i=85",
                cls=NodeClass.Object,
                children=(("i=47", "ns=2;s=Ack"),),
            ),
            "ns=2;s=Ack": _node(
                "ns=2;s=Ack",
                cls=NodeClass.Method,
                children=(("i=46", "ns=2;s=Ack/InputArguments"),),
            ),
            "ns=2;s=Ack/InputArguments": _node("ns=2;s=Ack/InputArguments", cls=NodeClass.Variable),
        },
        types={},
    )
    cfg = BrowseConfig(browse_variable_properties=True)
    result = await browse(source, connection="conn_ref", cfg=cfg)
    by_id = {n.node_id: n for n in result.nodes}
    assert by_id["ns=2;s=Ack"].node_class is NodeClass.Method
    assert by_id["ns=2;s=Ack"].is_composition is False
    assert by_id["ns=2;s=Ack/InputArguments"].parent_relationship == "HasProperty"


async def test_hasproperty_children_omitted_when_flag_off() -> None:
    """When browse_variable_properties=False (default), Variable's
    HasProperty children are not re-browsed and don't appear in walk
    output. The Variable itself stays non-composition."""
    source = _FakeSource(
        ns_array=["urn:demo"],
        nodes={
            "ns=2;s=Boiler": _node(
                "ns=2;s=Boiler",
                cls=NodeClass.Object,
                children=(("i=47", "ns=2;s=Boiler/AckedState"),),
            ),
            "ns=2;s=Boiler/AckedState": _node(
                "ns=2;s=Boiler/AckedState",
                cls=NodeClass.Variable,
                ns="urn:demo",
                children=(("i=46", "ns=2;s=Boiler/AckedState/Id"),),
            ),
            "ns=2;s=Boiler/AckedState/Id": _node(
                "ns=2;s=Boiler/AckedState/Id",
                cls=NodeClass.Variable,
                ns="urn:demo",
            ),
        },
        types={},
    )
    cfg = BrowseConfig(
        instance_roots=("ns=2;s=Boiler",),
        type_roots=(),
        # browse_variable_properties=False is the default.
    )
    result = await browse(source, connection="test", cfg=cfg)
    by_id = {n.node_id: n for n in result.nodes}
    assert "ns=2;s=Boiler/AckedState" in by_id
    assert by_id["ns=2;s=Boiler/AckedState"].is_composition is False
    assert "ns=2;s=Boiler/AckedState/Id" not in by_id


async def test_method_inputarguments_omitted_when_flag_off() -> None:
    """When browse_variable_properties=False (default), Method's
    HasProperty children are not re-browsed."""
    source = _FakeSource(
        ns_array=["urn:demo"],
        nodes={
            "ns=2;s=Ack": _node(
                "ns=2;s=Ack",
                cls=NodeClass.Method,
                ns="urn:demo",
                children=(("i=46", "ns=2;s=Ack/InputArguments"),),
            ),
            "ns=2;s=Ack/InputArguments": _node(
                "ns=2;s=Ack/InputArguments",
                cls=NodeClass.Variable,
                ns="urn:demo",
            ),
        },
        types={},
    )
    cfg = BrowseConfig(
        instance_roots=("ns=2;s=Ack",),
        type_roots=(),
    )
    result = await browse(source, connection="test", cfg=cfg)
    by_id = {n.node_id: n for n in result.nodes}
    assert "ns=2;s=Ack" in by_id
    assert by_id["ns=2;s=Ack"].is_composition is False
    assert "ns=2;s=Ack/InputArguments" not in by_id


# ------------------------------------------------------------------ (c)


async def test_children_of_filters_by_relationship() -> None:
    reg = InstanceRegistry()
    parent = "conn_ref!ns=2;s=P"
    await reg.reconcile(
        [
            NodeDescriptor(
                node_id="ns=2;s=P",
                connection="conn_ref",
                display_name="P",
                node_class=NodeClass.Object,
                namespace_uri="urn:demo",
                type_source_id=None,
                parent_node_id=None,
                is_composition=True,
            ),
            NodeDescriptor(
                node_id="ns=2;s=P/Comp",
                connection="conn_ref",
                display_name="Comp",
                node_class=NodeClass.Variable,
                namespace_uri="urn:demo",
                type_source_id=None,
                parent_node_id="ns=2;s=P",
                is_composition=False,
                parent_relationship="HasComponent",
            ),
            NodeDescriptor(
                node_id="ns=2;s=P/Prop",
                connection="conn_ref",
                display_name="Prop",
                node_class=NodeClass.Variable,
                namespace_uri="urn:demo",
                type_source_id=None,
                parent_node_id="ns=2;s=P",
                is_composition=False,
                parent_relationship="HasProperty",
            ),
        ]
    )
    all_children = reg.children_of(parent)
    hc = reg.children_of(parent, relationship="HasComponent")
    hp = reg.children_of(parent, relationship="HasProperty")
    assert {c.display_name for c in all_children} == {"Comp", "Prop"}
    assert [c.display_name for c in hc] == ["Comp"]
    assert [c.display_name for c in hp] == ["Prop"]


# ------------------------------------------------------------------ (d)


def test_to_object_instance_emits_unknown_type_when_source_missing() -> None:
    node = NodeDescriptor(
        node_id="ns=3;i=1000319",
        connection="conn_ref",
        display_name="Acknowledge",
        node_class=NodeClass.Method,
        namespace_uri="http://test.org/UA/Data/",
        type_source_id=None,
        parent_node_id="ns=3;i=1000264",
        is_composition=False,
        parent_relationship="HasComponent",
    )
    oi = to_object_instance(node)
    assert oi.typeElementId == "conn_ref!UnknownType"


# ------------------------------------------------------------------ contract


@pytest.mark.contract
async def test_related_hasproperty_filter_returns_only_property_children(
    http_client: httpx.AsyncClient, app_state: AppState
) -> None:
    """POST /objects/related with relationshipType='HasProperty' returns only
    HasProperty-derived children; 'HasComponent' filters in the other
    direction; omission returns the full union."""
    # Extend the fixture with a HasProperty child of AckedState under Boiler1.
    nodes = list(app_state.instances.snapshot().values())
    nodes.extend(
        [
            NodeDescriptor(
                node_id="ns=2;s=Boiler1/AckedState",
                connection="conn_ref",
                display_name="AckedState",
                node_class=NodeClass.Variable,
                namespace_uri="urn:demo",
                type_source_id="ns=0;i=8995",
                parent_node_id="ns=2;s=Boiler1",
                is_composition=False,
                parent_relationship="HasComponent",
            ),
            NodeDescriptor(
                node_id="ns=2;s=Boiler1/AckedState/Id",
                connection="conn_ref",
                display_name="Id",
                node_class=NodeClass.Variable,
                namespace_uri="urn:demo",
                type_source_id="ns=0;i=1",
                parent_node_id="ns=2;s=Boiler1/AckedState",
                is_composition=False,
                parent_relationship="HasProperty",
            ),
        ]
    )
    await app_state.instances.reconcile(nodes)

    resp_hp = await http_client.post(
        "/v1/objects/related",
        headers={"Authorization": "Bearer test-token"},
        json={
            "elementIds": ["conn_ref!ns=2;s=Boiler1/AckedState"],
            "relationshipType": "HasProperty",
        },
    )
    body_hp = resp_hp.json()
    children_hp = body_hp["results"][0]["result"]
    assert [c["object"]["displayName"] for c in children_hp] == ["Id"]
    assert children_hp[0]["sourceRelationship"] == "HasProperty"

    resp_hc = await http_client.post(
        "/v1/objects/related",
        headers={"Authorization": "Bearer test-token"},
        json={
            "elementIds": ["conn_ref!ns=2;s=Boiler1/AckedState"],
            "relationshipType": "HasComponent",
        },
    )
    body_hc = resp_hc.json()
    assert body_hc["results"][0]["result"] == []

    resp_any = await http_client.post(
        "/v1/objects/related",
        headers={"Authorization": "Bearer test-token"},
        json={"elementIds": ["conn_ref!ns=2;s=Boiler1/AckedState"]},
    )
    body_any = resp_any.json()
    children_any = body_any["results"][0]["result"]
    assert [c["object"]["displayName"] for c in children_any] == ["Id"]
