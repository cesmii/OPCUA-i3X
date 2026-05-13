"""D-72 — lazy artificial-type registration: one TypeDescriptor per unique
(DataType, Rank, Access) triple observed across walked Variables."""

from __future__ import annotations

from typing import Any

from i3xua.adapters.asyncua.browse import collect_artificial_types
from i3xua.core.neutral import NodeClass, NodeDescriptor


def _var(**overrides: Any) -> NodeDescriptor:
    base: dict[str, Any] = dict(
        node_id="ns=2;s=T1",
        connection="kepware",
        display_name="T1",
        node_class=NodeClass.Variable,
        namespace_uri="urn:demo",
        type_source_id="i=63",  # generic — eligible for replacement
        parent_node_id=None,
        is_composition=False,
        data_type_node_id="i=11",
        access_level=1,
        user_access_level=1,
        value_rank=-1,
        array_dimensions=None,
        historizing=False,
        minimum_sampling_interval=10.0,
    )
    base.update(overrides)
    return NodeDescriptor(**base)


def test_observed_shapes_register_one_descriptor_per_unique_triple() -> None:
    nodes = [
        _var(node_id="ns=2;s=A", data_type_node_id="i=11"),  # Double_Scalar_RO
        _var(node_id="ns=2;s=B", data_type_node_id="i=11"),  # Double_Scalar_RO (dup)
        _var(node_id="ns=2;s=C", data_type_node_id="i=12"),  # String_Scalar_RO
        _var(node_id="ns=2;s=D", data_type_node_id="i=11", access_level=3),  # Double_Scalar_RW
    ]
    types = collect_artificial_types(nodes)
    names = {t.display_name for t in types}
    assert names == {"Double_Scalar_RO", "String_Scalar_RO", "Double_Scalar_RW"}
    # D-70 amended: descriptors live under the SOURCE connection's
    # namespace, not a global pseudo-connection.
    assert all(t.connection == "kepware" for t in types)
    assert all(t.namespace_uri == "urn:demo" for t in types)


def test_non_generic_server_types_are_skipped() -> None:
    """Variables typed AnalogItemType (etc.) pass through with their server
    type intact — the wrapper only synthesizes when the server-reported
    type is one of the generic placeholders."""
    nodes = [
        _var(type_source_id="i=2368"),  # AnalogItemType — pass through
        _var(type_source_id="i=63", data_type_node_id="i=11"),
    ]
    types = collect_artificial_types(nodes)
    names = {t.display_name for t in types}
    assert names == {"Double_Scalar_RO"}


def test_variables_missing_attributes_are_skipped() -> None:
    """No partial artificial types — we need DataType + ValueRank + AccessLevel."""
    nodes = [_var(data_type_node_id=None)]
    assert collect_artificial_types(nodes) == []

    nodes = [_var(value_rank=None)]
    assert collect_artificial_types(nodes) == []

    nodes = [_var(access_level=None)]
    assert collect_artificial_types(nodes) == []


def test_artificial_descriptor_schema_for_double_scalar() -> None:
    nodes = [_var(data_type_node_id="i=11", value_rank=-1, access_level=1)]
    types = collect_artificial_types(nodes)
    assert len(types) == 1
    schema = types[0].json_schema
    assert schema["type"] == "object"
    assert schema["properties"]["value"] == {"type": "number"}


def test_artificial_descriptor_schema_for_uint32_array() -> None:
    nodes = [_var(data_type_node_id="i=7", value_rank=1, access_level=3)]
    types = collect_artificial_types(nodes)
    assert len(types) == 1
    value_schema = types[0].json_schema["properties"]["value"]
    assert value_schema["type"] == "array"
    assert value_schema["items"]["type"] == "integer"


def test_artificial_descriptor_source_node_id_is_the_artificial_name() -> None:
    """`source_node_id` doubles as the elementId NodeId-segment, so the
    instance's `typeElementId` resolves cleanly when mapping prepends
    `<connection>!` (D-70 amended — co-located with source connection)."""
    nodes = [_var(data_type_node_id="i=11", value_rank=-1, access_level=1)]
    types = collect_artificial_types(nodes)
    assert types[0].source_node_id == "Double_Scalar_RO"
    assert types[0].structural_hash == "artificial:kepware:Double_Scalar_RO"


def test_object_nodes_are_skipped_even_when_eligible_type() -> None:
    """Only Variables get artificial types — Objects never do, regardless
    of their type_source_id."""
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
    assert collect_artificial_types([obj]) == []
