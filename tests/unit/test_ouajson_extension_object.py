"""\u2014 ExtensionObject Part-6 encoding.

Part 6 §5.4.2.12: `{TypeId, Body}` where Body is the structure encoded field
by field per its StructureDefinition.
"""

from __future__ import annotations

from datetime import UTC, datetime

from i3xua.ouajson import (
    ExtensionObject,
    NodeId,
    StructureDefinition,
    StructureField,
    Variant,
    VariantType,
    encode_extension_object,
    encode_variant,
)

BOILER_STATE = StructureDefinition(
    type_id=NodeId(id="BoilerStateType", namespace=2),
    fields=(
        StructureField(name="Temperature", data_type_node_id="ns=0;i=11"),
        StructureField(name="State", data_type_node_id="ns=0;i=12"),
        StructureField(name="Since", data_type_node_id="ns=0;i=13"),
        StructureField(name="Labels", data_type_node_id="ns=0;i=12", value_rank=1),
        StructureField(name="Note", data_type_node_id="ns=0;i=12", is_optional=True),
    ),
)


def test_extension_object_emits_type_id_and_body() -> None:
    eo = ExtensionObject(
        definition=BOILER_STATE,
        fields={
            "Temperature": 88.4,
            "State": "Running",
            "Since": datetime(2026, 4, 14, 1, 2, 3, tzinfo=UTC),
            "Labels": ["plant-a", "line-3"],
        },
    )
    encoded = encode_extension_object(eo)
    assert encoded == {
        "TypeId": {"IdType": 1, "Id": "BoilerStateType", "Namespace": 2},
        "Body": {
            "Temperature": 88.4,
            "State": "Running",
            "Since": "2026-04-14T01:02:03Z",
            "Labels": ["plant-a", "line-3"],
        },
    }


def test_optional_field_absent_is_omitted() -> None:
    eo = ExtensionObject(
        definition=BOILER_STATE,
        fields={
            "Temperature": 1.0,
            "State": "Off",
            "Since": datetime(2026, 4, 14, tzinfo=UTC),
            "Labels": [],
        },
    )
    encoded = encode_extension_object(eo)
    assert "Note" not in encoded["Body"]


def test_nested_extension_object_is_recursively_encoded() -> None:
    inner_def = StructureDefinition(
        type_id=NodeId(id="Inner", namespace=2),
        fields=(StructureField(name="x", data_type_node_id="ns=0;i=6"),),
    )
    outer_def = StructureDefinition(
        type_id=NodeId(id="Outer", namespace=2),
        fields=(
            StructureField(name="label", data_type_node_id="ns=0;i=12"),
            StructureField(name="inner", data_type_node_id="ns=2;s=Inner"),
        ),
    )
    eo = ExtensionObject(
        definition=outer_def,
        fields={
            "label": "hi",
            "inner": ExtensionObject(definition=inner_def, fields={"x": 7}),
        },
    )
    encoded = encode_extension_object(eo)
    assert encoded["Body"]["inner"] == {
        "TypeId": {"IdType": 1, "Id": "Inner", "Namespace": 2},
        "Body": {"x": 7},
    }


def test_variant_of_extension_object_dispatches_through_encode_variant() -> None:
    eo = ExtensionObject(
        definition=BOILER_STATE,
        fields={
            "Temperature": 10.0,
            "State": "Stopped",
            "Since": datetime(2026, 4, 14, tzinfo=UTC),
            "Labels": [],
        },
    )
    encoded = encode_variant(Variant(VariantType.ExtensionObject, eo))
    assert encoded["Type"] == int(VariantType.ExtensionObject)
    assert encoded["Body"]["TypeId"] == {"IdType": 1, "Id": "BoilerStateType", "Namespace": 2}
    assert "Temperature" in encoded["Body"]["Body"]
