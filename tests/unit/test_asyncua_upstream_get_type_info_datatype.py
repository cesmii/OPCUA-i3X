"""upstream.get_type_info reads DataType for VariableType nodes."""

from __future__ import annotations

import asyncio
from typing import Any

from asyncua import ua


class _FakeNode:
    def __init__(
        self,
        *,
        node_id: ua.NodeId,
        display_name: str = "FakeType",
        node_class: ua.NodeClass = ua.NodeClass.ObjectType,
        data_type: ua.NodeId | None = None,
        subtypes: tuple[ua.NodeId, ...] = (),
        component_refs: tuple[Any, ...] = (),
        property_refs: tuple[Any, ...] = (),
    ) -> None:
        self._node_id = node_id
        self._display_name = display_name
        self._node_class = node_class
        self._data_type = data_type
        self._subtypes = subtypes
        self._component_refs = component_refs
        self._property_refs = property_refs

    async def read_display_name(self) -> ua.LocalizedText:
        return ua.LocalizedText(Text=self._display_name)

    async def read_node_class(self) -> ua.NodeClass:
        return self._node_class

    async def read_data_type(self) -> ua.NodeId:
        if self._data_type is None:
            raise Exception("no DataType attribute")
        return self._data_type

    async def get_references(self, refs: ua.NodeId, direction: Any) -> list[Any]:
        if refs == ua.NodeId(45, 0):  # HasSubtype
            return [type("R", (), {"NodeId": s})() for s in self._subtypes]
        if refs == ua.NodeId(47, 0):  # HasComponent
            return list(self._component_refs)
        if refs == ua.NodeId(46, 0):  # HasProperty
            return list(self._property_refs)
        return []


class _FakeClient:
    def __init__(self, nodes: dict[str, _FakeNode]) -> None:
        self._nodes = nodes

    def get_node(self, node_id: ua.NodeId) -> _FakeNode:
        return self._nodes[node_id.to_string()]

    async def get_namespace_array(self) -> list[str]:
        return ["http://opcfoundation.org/UA/"]


def _make_source_with(nodes: dict[str, _FakeNode]) -> Any:
    """Construct an _AsyncuaBrowseSource bypassing __init__ and inject the fake client."""
    from i3xua.adapters.asyncua.upstream import (
        _AsyncuaBrowseSource as _Source,  # type: ignore
    )

    src = _Source.__new__(_Source)
    src._client = _FakeClient(nodes)  # type: ignore[attr-defined]
    return src


def test_get_type_info_includes_data_type_for_variabletype() -> None:
    nid = ua.NodeId(2368, 0)
    node = _FakeNode(
        node_id=nid,
        display_name="AnalogItemType",
        node_class=ua.NodeClass.VariableType,
        data_type=ua.NodeId(11, 0),
    )
    src = _make_source_with({"i=2368": node})
    info = asyncio.run(src.get_type_info("i=2368"))
    assert info is not None
    assert info.data_type_node_id == "i=11"


def test_get_type_info_omits_data_type_for_objecttype() -> None:
    nid = ua.NodeId(58, 0)
    node = _FakeNode(
        node_id=nid,
        display_name="BaseObjectType",
        node_class=ua.NodeClass.ObjectType,
    )
    src = _make_source_with({"i=58": node})
    info = asyncio.run(src.get_type_info("i=58"))
    assert info is not None
    assert info.data_type_node_id is None
