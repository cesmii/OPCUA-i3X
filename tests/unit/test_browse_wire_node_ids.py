"""D-66 wire convention — opaque numeric NodeIds become display-paths on
the wire; string NodeIds keep their canonical form."""

from __future__ import annotations

from i3xua.adapters.asyncua.browse import _stamp_wire_node_ids
from i3xua.core.neutral import NodeClass, NodeDescriptor


def _node(
    *, node_id: str, display_name: str, parent_node_id: str | None = None
) -> NodeDescriptor:
    return NodeDescriptor(
        node_id=node_id,
        connection="conn",
        display_name=display_name,
        node_class=NodeClass.Object,
        namespace_uri="urn:demo",
        type_source_id=None,
        parent_node_id=parent_node_id,
        is_composition=False,
    )


def test_string_node_ids_keep_canonical_form() -> None:
    """Kepware-style `ns=2;s=Channel1.Device1.Tag1` is already readable;
    leave it alone."""
    nodes = [
        _node(node_id="ns=0;i=85", display_name="ThingWorx Kepware Server"),
        _node(
            node_id="ns=2;s=Channel1.Device1.Tag1",
            display_name="Tag1",
            parent_node_id="ns=0;i=85",
        ),
    ]
    stamped = _stamp_wire_node_ids(nodes)
    by_id = {n.node_id: n for n in stamped}
    # Root has numeric NodeId but no parent path → falls back to its NodeId.
    assert by_id["ns=0;i=85"].wire_node_id == "ns=0;i=85"
    # Kepware tag keeps its string NodeId form.
    assert by_id["ns=2;s=Channel1.Device1.Tag1"].wire_node_id == "ns=2;s=Channel1.Device1.Tag1"


def test_numeric_node_ids_become_display_path() -> None:
    """ua-ref-server-style numeric NodeIds get a display-path replacement
    (root segment dropped — already implicit in the connection prefix)."""
    nodes = [
        _node(node_id="ns=0;i=85", display_name="Quickstart Reference Server"),
        _node(node_id="ns=4;i=1238", display_name="Boilers", parent_node_id="ns=0;i=85"),
        _node(node_id="ns=4;i=1239", display_name="Boiler #1", parent_node_id="ns=4;i=1238"),
        _node(node_id="ns=5;i=1240", display_name="Pipe1001", parent_node_id="ns=4;i=1239"),
        _node(node_id="ns=5;i=1241", display_name="FTX001", parent_node_id="ns=5;i=1240"),
    ]
    stamped = _stamp_wire_node_ids(nodes)
    by_id = {n.node_id: n for n in stamped}
    assert by_id["ns=4;i=1238"].wire_node_id == "Boilers"
    assert by_id["ns=4;i=1239"].wire_node_id == "Boilers.Boiler #1"
    assert by_id["ns=5;i=1240"].wire_node_id == "Boilers.Boiler #1.Pipe1001"
    assert by_id["ns=5;i=1241"].wire_node_id == "Boilers.Boiler #1.Pipe1001.FTX001"


def test_parent_wire_node_id_stamped() -> None:
    """`parent_wire_node_id` lets `InstanceRegistry.children_of` filter by
    the wire-side elementId without a second registry lookup."""
    nodes = [
        _node(node_id="ns=0;i=85", display_name="Server"),
        _node(node_id="ns=4;i=1238", display_name="Boilers", parent_node_id="ns=0;i=85"),
        _node(node_id="ns=5;i=1240", display_name="Pipe1001", parent_node_id="ns=4;i=1238"),
    ]
    stamped = _stamp_wire_node_ids(nodes)
    by_id = {n.node_id: n for n in stamped}
    # Root has no parent.
    assert by_id["ns=0;i=85"].parent_wire_node_id == ""
    # Top-level child's parent is the root — root contributes no path.
    assert by_id["ns=4;i=1238"].parent_wire_node_id == "ns=0;i=85"
    # Pipe1001's parent_wire is the Boilers wire form (display-path).
    assert by_id["ns=5;i=1240"].parent_wire_node_id == "Boilers"


def test_collision_disambiguated_with_raw_node_id() -> None:
    """OPC UA only enforces BrowseName uniqueness among siblings —
    DisplayName can repeat. Two children with the same display_name
    under the same parent get distinct wire forms via raw-NodeId
    suffix so the InstanceRegistry doesn't silently drop duplicates."""
    nodes = [
        _node(node_id="ns=0;i=85", display_name="Server"),
        _node(node_id="ns=4;i=100", display_name="Group", parent_node_id="ns=0;i=85"),
        _node(node_id="ns=4;i=200", display_name="Value", parent_node_id="ns=4;i=100"),
        _node(node_id="ns=4;i=201", display_name="Value", parent_node_id="ns=4;i=100"),
    ]
    stamped = _stamp_wire_node_ids(nodes)
    by_id = {n.node_id: n for n in stamped}
    assert by_id["ns=4;i=200"].wire_node_id == "Group.Value"
    # Second sibling collides → disambiguator suffix.
    assert by_id["ns=4;i=201"].wire_node_id == "Group.Value#ns=4;i=201"
    # Both wire forms must be distinct (registry won't drop either).
    assert by_id["ns=4;i=200"].wire_node_id != by_id["ns=4;i=201"].wire_node_id


def test_mixed_string_and_numeric_under_string_parent() -> None:
    """A numeric NodeId child of a string-NodeId parent still gets a
    display-path wire form (path is built from display_names regardless of
    the parent's wire form)."""
    nodes = [
        _node(node_id="ns=2;s=Server", display_name="Server"),
        _node(node_id="ns=2;s=Channel1", display_name="Channel1", parent_node_id="ns=2;s=Server"),
        _node(node_id="ns=2;i=999", display_name="OddChild", parent_node_id="ns=2;s=Channel1"),
    ]
    stamped = _stamp_wire_node_ids(nodes)
    by_id = {n.node_id: n for n in stamped}
    assert by_id["ns=2;s=Channel1"].wire_node_id == "ns=2;s=Channel1"
    # The numeric child gets the display path — built from display_name
    # chain, not from the parent's wire form.
    assert by_id["ns=2;i=999"].wire_node_id == "Channel1.OddChild"
