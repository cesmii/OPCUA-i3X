"""Browse stamps NodeDescriptor.data_type_node_id from NodeInfo for Variables."""

from __future__ import annotations

import asyncio

from i3xua.adapters.asyncua.browse import (
    BrowsedChild,
    NodeInfo,
    TypeInfo,
    _browse_instances,
)
from i3xua.core.neutral import NodeClass


class _FakeSource:
    def __init__(self, nodes: dict[str, NodeInfo], children_map: dict[str, list[str]]) -> None:
        self._nodes = nodes
        self._children = children_map

    async def get_namespace_array(self) -> list[str]:
        return ["http://opcfoundation.org/UA/", "http://example.org/UA/"]

    async def get_node_info(self, node_id: str) -> NodeInfo | None:
        return self._nodes.get(node_id)

    async def get_node_info_batch(self, node_ids: list[str]) -> list[NodeInfo | None]:
        return [self._nodes.get(nid) for nid in node_ids]

    async def browse_children(self, parent_node_ids: list[str]) -> list[BrowsedChild]:
        out: list[BrowsedChild] = []
        for pid in parent_node_ids:
            for kid in self._children.get(pid, []):
                if kid in self._nodes:
                    out.append(
                        BrowsedChild(
                            info=self._nodes[kid],
                            parent_node_id=pid,
                            parent_relationship="HasComponent",
                        )
                    )
        return out

    async def get_type_info(self, node_id: str) -> TypeInfo | None:
        return None


def test_browse_stamps_data_type_node_id_on_variable() -> None:
    objects = NodeInfo(
        node_id="ns=0;i=85",
        browse_name="Objects",
        display_name="Objects",
        node_class=NodeClass.Object,
        namespace_uri="http://opcfoundation.org/UA/",
        type_definition_node_id="i=61",
    )
    output = NodeInfo(
        node_id="ns=1;i=1242",
        browse_name="1:Output",
        display_name="Output",
        node_class=NodeClass.Variable,
        namespace_uri="http://example.org/UA/",
        type_definition_node_id="i=2368",
        data_type_node_id="i=11",
    )
    src = _FakeSource(
        nodes={"ns=0;i=85": objects, "ns=1;i=1242": output},
        children_map={"ns=0;i=85": ["ns=1;i=1242"]},
    )
    out = asyncio.run(
        _browse_instances(
            src,
            connection="conn",
            roots=("ns=0;i=85",),
            namespace_allowlist=None,
            max_parents_per_batch=10,
            max_children_per_batch=100,
            browse_variable_properties=False,
        )
    )
    by_id = {n.node_id: n for n in out}
    assert by_id["ns=1;i=1242"].data_type_node_id == "i=11"
    assert by_id["ns=1;i=1242"].type_source_id == "i=2368"
    assert by_id["ns=0;i=85"].data_type_node_id is None
