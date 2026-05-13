"""D-69/D-73 — `to_object_instance.typeElementId` swaps to artificial when
the server reports a generic TypeDefinition AND the per-connection kill
switch is on AND the Variable carries the attributes needed to derive a
shape."""

from __future__ import annotations

from typing import Any

from i3xua.core.mapping import to_object_instance
from i3xua.core.neutral import NodeClass, NodeDescriptor


def _generic_var(**overrides: Any) -> NodeDescriptor:
    base: dict[str, Any] = dict(
        node_id="ns=2;s=Tag1",
        connection="kepware",
        display_name="Tag1",
        node_class=NodeClass.Variable,
        namespace_uri="urn:demo",
        type_source_id="i=63",  # BaseDataVariableType
        parent_node_id="ns=2;s=Channel1",
        is_composition=False,
        data_type_node_id="i=11",
        access_level=1,
        user_access_level=1,
        value_rank=-1,
        historizing=False,
        minimum_sampling_interval=10.0,
    )
    base.update(overrides)
    return NodeDescriptor(**base)


def test_typeElementId_replaced_by_artificial_when_enabled() -> None:
    inst = to_object_instance(_generic_var(), artificial_types_enabled=True)
    assert inst.typeElementId == "kepware!Double_Scalar_RO"


def test_typeElementId_preserved_when_artificial_disabled() -> None:
    """The kill switch (D-73) leaves the server-reported type untouched."""
    inst = to_object_instance(_generic_var(), artificial_types_enabled=False)
    assert inst.typeElementId == "kepware!i=63"


def test_typeElementId_preserved_when_server_type_not_generic() -> None:
    """AnalogItemType (and other non-generic server types) pass through
    even when the kill switch is on."""
    inst = to_object_instance(_generic_var(type_source_id="i=2368"), artificial_types_enabled=True)
    assert inst.typeElementId == "kepware!i=2368"


def test_typeElementId_preserved_when_attributes_incomplete() -> None:
    """Variable typed BDVT but missing AccessLevel — no replacement."""
    inst = to_object_instance(_generic_var(access_level=None), artificial_types_enabled=True)
    assert inst.typeElementId == "kepware!i=63"


def test_typeElementId_preserved_when_value_rank_missing() -> None:
    inst = to_object_instance(_generic_var(value_rank=None), artificial_types_enabled=True)
    assert inst.typeElementId == "kepware!i=63"


def test_typeElementId_preserved_when_data_type_missing() -> None:
    inst = to_object_instance(_generic_var(data_type_node_id=None), artificial_types_enabled=True)
    assert inst.typeElementId == "kepware!i=63"


def test_typeElementId_propertytype_passes_through() -> None:
    """PropertyType (`i=68`) keeps its server-reported type — Properties
    are structurally meaningful and the parent type already carries the
    context (D-69 amended)."""
    inst = to_object_instance(_generic_var(type_source_id="i=68"), artificial_types_enabled=True)
    assert inst.typeElementId == "kepware!i=68"


def test_typeElementId_default_kwarg_is_artificial_enabled() -> None:
    """Default behavior — no `artificial_types_enabled` kwarg — replaces
    generic types. Routes that haven't been wired through fall into this
    same default."""
    inst = to_object_instance(_generic_var())
    assert inst.typeElementId == "kepware!Double_Scalar_RO"


def test_object_typeElementId_unaffected_by_artificial_swap() -> None:
    """Objects never get artificial types, even if their type_source_id
    happens to be in the generic set (won't happen in practice, but the
    invariant is Variable-only)."""
    obj = NodeDescriptor(
        node_id="ns=2;s=Channel1",
        connection="kepware",
        display_name="Channel1",
        node_class=NodeClass.Object,
        namespace_uri="urn:demo",
        type_source_id="i=63",
        parent_node_id=None,
        is_composition=True,
    )
    inst = to_object_instance(obj, artificial_types_enabled=True)
    assert inst.typeElementId == "kepware!i=63"
