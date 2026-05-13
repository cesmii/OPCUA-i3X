"""`isExtended` + `metadata.extendedAttributes` on instances of types
with unresolvable fields."""

from __future__ import annotations

from i3xua.adapters.asyncua.typemap import (
    structure_to_json_schema_with_unresolved,
)
from i3xua.core.mapping import to_object_instance, to_object_instances
from i3xua.core.neutral import (
    NodeClass,
    NodeDescriptor,
    TypeDescriptor,
)


def _node(type_source_id: str | None = "ns=2;s=WeirdType") -> NodeDescriptor:
    return NodeDescriptor(
        node_id="ns=2;s=Boiler1",
        connection="conn_ref",
        display_name="Boiler1",
        node_class=NodeClass.Object,
        namespace_uri="urn:demo",
        type_source_id=type_source_id,
        parent_node_id=None,
        is_composition=True,
    )


def _type(*, unresolved: tuple[str, ...] = ()) -> TypeDescriptor:
    return TypeDescriptor(
        source_node_id="ns=2;s=WeirdType",
        display_name="WeirdType",
        namespace_uri="urn:demo",
        connection="conn_ref",
        structural_hash="abc",
        json_schema={"type": "object", "properties": {}},
        unresolved_fields=unresolved,
    )


# ------------------------------------------------------------------ typemap -> unresolved


def test_schema_with_unresolved_reports_unknown_fields() -> None:
    fields = [
        ("Temp", "ns=0;i=11", -1, False),
        ("Kind", "ns=2;s=CustomEnum", -1, False),
        ("State", "ns=0;i=29", -1, True),
        ("Blob", "ns=2;s=Weird", -1, True),
    ]
    schema, unresolved = structure_to_json_schema_with_unresolved(fields)
    assert schema["properties"]["Temp"]["type"] == "number"
    assert "State" not in unresolved
    assert set(unresolved) == {"Kind", "Blob"}


def test_schema_with_unresolved_respects_resolver_hits() -> None:
    def resolver(nid: str) -> dict | None:
        if nid == "ns=2;s=CustomEnum":
            return {"type": "integer", "enum": [0, 1]}
        return None

    fields = [
        ("Kind", "ns=2;s=CustomEnum", -1, False),
        ("Blob", "ns=2;s=Weird", -1, True),
    ]
    schema, unresolved = structure_to_json_schema_with_unresolved(fields, resolver=resolver)
    assert schema["properties"]["Kind"]["enum"] == [0, 1]
    assert unresolved == ("Blob",)


# ------------------------------------------------------------------ to_object_instance


def test_instance_without_types_has_no_extendedAttributes() -> None:
    inst = to_object_instance(_node())
    assert inst.isExtended is False
    assert inst.metadata is not None
    assert inst.metadata.extendedAttributes is None


def test_instance_with_type_but_no_unresolved_fields_is_not_extended() -> None:
    types_map = {"ns=2;s=WeirdType": _type(unresolved=())}
    inst = to_object_instance(_node(), types=types_map)
    assert inst.isExtended is False
    assert inst.metadata is not None
    assert inst.metadata.extendedAttributes is None


def test_instance_with_unresolved_fields_marks_isExtended_and_populates_attrs() -> None:
    types_map = {"ns=2;s=WeirdType": _type(unresolved=("Kind", "Blob"))}
    inst = to_object_instance(_node(), types=types_map)
    assert inst.isExtended is True
    assert inst.metadata is not None
    ext = inst.metadata.extendedAttributes
    assert ext is not None
    assert set(ext) == {"Kind", "Blob"}
    for key in ("Kind", "Blob"):
        assert ext[key]["type"] == "string"
        assert "CSV" in ext[key]["description"]


def test_instance_without_type_source_id_is_not_extended() -> None:
    types_map = {"ns=2;s=WeirdType": _type(unresolved=("Kind",))}
    node = _node(type_source_id=None)
    inst = to_object_instance(node, types=types_map)
    assert inst.isExtended is False


def test_to_object_instances_propagates_types_map() -> None:
    types_map = {"ns=2;s=WeirdType": _type(unresolved=("Kind",))}
    out = to_object_instances([_node(), _node()], types=types_map)
    assert all(i.isExtended is True for i in out)
