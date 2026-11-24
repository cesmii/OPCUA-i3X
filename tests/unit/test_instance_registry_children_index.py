"""children_of uses a parent index rebuilt per registry swap.

A linear scan per call made GET /objects O(n^2): on a 772K-instance Kepware
the listing never finished and blocked the event loop.
"""

from __future__ import annotations

import time

from i3xua.core.neutral import NodeClass, NodeDescriptor
from i3xua.core.registry import InstanceRegistry


def _node(
    nid: str, parent: str | None, rel: str = "HasComponent", conn: str = "c1"
) -> NodeDescriptor:
    return NodeDescriptor(
        node_id=nid,
        connection=conn,
        display_name=nid,
        node_class=NodeClass.Variable if parent else NodeClass.Object,
        namespace_uri="urn:test",
        type_source_id=None,
        parent_node_id=parent,
        is_composition=False,
        parent_relationship=rel if parent else "",
    )


def _registry(nodes: list[NodeDescriptor]) -> InstanceRegistry:
    reg = InstanceRegistry()
    reg.replace_sync({f"{n.connection}!{n.node_id}": n for n in nodes})
    return reg


def test_children_filters_connection_and_relationship() -> None:
    reg = _registry(
        [
            _node("root", None),
            _node("a", "root"),
            _node("b", "root", rel="HasProperty"),
            _node("x", "root", conn="c2"),
        ]
    )
    assert [n.node_id for n in reg.children_of("c1!root")] == ["a", "b"]
    assert [n.node_id for n in reg.children_of("c1!root", "HasProperty")] == ["b"]
    assert [n.node_id for n in reg.children_of("c2!root")] == ["x"]
    assert [n.node_id for n in reg.children_of(None)] == ["root"]
    assert reg.children_of("c1!missing") == []


async def test_index_follows_registry_writes() -> None:
    reg = _registry([_node("root", None), _node("a", "root")])
    assert [n.node_id for n in reg.children_of("c1!root")] == ["a"]
    await reg.update(added={"c1!b": _node("b", "root")}, removed=["c1!a"])
    assert [n.node_id for n in reg.children_of("c1!root")] == ["b"]


def test_children_of_every_node_is_linear() -> None:
    n_folders, per_folder = 200, 1000  # 200K instances
    nodes = [_node(f"f{i}", None) for i in range(n_folders)]
    nodes += [_node(f"f{i}.t{j}", f"f{i}") for i in range(n_folders) for j in range(per_folder)]
    reg = _registry(nodes)
    t0 = time.monotonic()
    total = sum(len(reg.children_of(f"c1!{n.node_id}")) for n in nodes)
    assert total == n_folders * per_folder
    assert time.monotonic() - t0 < 5.0  # the O(n^2) scan took hours here
