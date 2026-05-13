"""Every typeElementId on an instance resolves to a registered ObjectType."""

from __future__ import annotations

from i3xua.core.mapping import to_object_instance, to_object_type
from i3xua.core.neutral import NodeClass, NodeDescriptor, TypeDescriptor


def _instance(*, type_source_id: str | None) -> NodeDescriptor:
    return NodeDescriptor(
        node_id="ns=1;i=100",
        connection="conn",
        display_name="X",
        node_class=NodeClass.Variable,
        namespace_uri="http://example.org/UA/",
        type_source_id=type_source_id,
        parent_node_id=None,
        is_composition=False,
        data_type_node_id="i=11",
    )


def _type(node_id: str, name: str = "T") -> TypeDescriptor:
    return TypeDescriptor(
        source_node_id=node_id,
        display_name=name,
        namespace_uri="http://example.org/UA/",
        connection="conn",
        structural_hash="abc",
        json_schema={"type": "object"},
    )


def test_all_typeElementIds_resolve_to_registered_or_unknown() -> None:
    """Build a small fixture (1 type + UnknownType, 2 instances), verify every
    typeElementId resolves to a registered ObjectType including the UnknownType
    placeholder."""
    types = [_type("i=2368", "AnalogItemType"), _type("UnknownType", "UnknownType")]
    nodes = [_instance(type_source_id="i=2368"), _instance(type_source_id=None)]

    type_map = {t.source_node_id: t for t in types}

    type_payloads = [to_object_type(t) for t in types]
    type_element_ids = {ot.elementId for ot in type_payloads}

    instance_payloads = [to_object_instance(n, types=type_map) for n in nodes]
    for inst in instance_payloads:
        assert inst.typeElementId in type_element_ids, (
            f"dangling typeElementId: {inst.typeElementId!r} not in {sorted(type_element_ids)!r}"
        )
