"""Mapping from neutral adapter types to i3X wire models."""

from __future__ import annotations

from i3xua.core.mapping import (
    QUALITY_SYMBOL_MAP,
    status_symbol_to_quality,
    to_composition_value,
    to_current_value,
    to_empty_value,
    to_historical_value,
    to_namespace,
    to_namespaces,
    to_object_instance,
    to_object_type,
)
from i3xua.core.neutral import (
    NamespaceInfo,
    NodeClass,
    NodeDescriptor,
    Quality,
    TypeDescriptor,
    ValueSample,
)
from i3xua.i3x.types import VQT

# ------------------------------------------------------------------ namespaces


def test_namespace_passes_through_to_wire() -> None:
    """to_namespace just hands the URI and displayName onto the wire;
    the URI carries the per-connection collision suffix (D-34) that
    the browse layer composed."""
    ns = to_namespace(
        NamespaceInfo(
            uri="urn:vendor:demo#connection=conn_a",
            connection="conn_a",
            display_name="demo-ns2",
        )
    )
    assert ns.uri == "urn:vendor:demo#connection=conn_a"
    assert ns.displayName == "demo-ns2"


def test_namespaces_batch() -> None:
    infos = [
        NamespaceInfo(uri="urn:s1#connection=c1", connection="c1", display_name="s1-ns0"),
        NamespaceInfo(uri="urn:s2#connection=c2", connection="c2", display_name="s2-ns0"),
    ]
    result = to_namespaces(infos)
    assert {n.uri for n in result} == {"urn:s1#connection=c1", "urn:s2#connection=c2"}


# ------------------------------------------------------------------ object types


def test_object_type_carries_source_id_and_collision_safe_namespace() -> None:
    t = TypeDescriptor(
        source_node_id="ns=2;s=BoilerType",
        display_name="BoilerType",
        namespace_uri="urn:demo",
        connection="conn_ref",
        structural_hash="abc",
        json_schema={"type": "object", "properties": {"Temp": {"type": "number"}}},
        application_uri="urn:vendor:demo-server",
    )
    ot = to_object_type(t)
    # elementId NodeId-segment is the human-readable display_name (D-69 amended).
    assert ot.elementId == "conn_ref!BoilerType"
    assert ot.sourceTypeId == "BoilerType"
    # namespaceUri is the OPC UA namespace with the per-connection
    # collision suffix (D-34) — matches what /v1/namespaces emits.
    assert ot.namespaceUri == "urn:demo#connection=conn_ref"
    assert ot.schema_["properties"]["Temp"]["type"] == "number"


# ------------------------------------------------------------------ object instances


def test_object_instance_prefixes_element_parent_and_type_ids() -> None:
    node = NodeDescriptor(
        node_id="ns=2;s=Boiler1",
        connection="conn_ref",
        display_name="Boiler1",
        node_class=NodeClass.Object,
        namespace_uri="urn:demo",
        type_source_id="ns=2;s=BoilerType",
        parent_node_id="ns=0;i=85",
        is_composition=True,
    )
    oi = to_object_instance(node)
    assert oi.elementId == "conn_ref!ns=2;s=Boiler1"
    assert oi.parentId == "conn_ref!ns=0;i=85"
    assert oi.typeElementId == "conn_ref!ns=2;s=BoilerType"
    assert oi.isComposition is True
    assert oi.metadata is not None
    assert oi.metadata.system is not None
    assert oi.metadata.system["nodeClass"] == "Object"
    # No types map provided → falls back to NodeId form.
    assert oi.metadata.sourceTypeId == "ns=2;s=BoilerType"


def test_object_instance_sourceTypeId_resolves_to_display_name_via_types_map() -> None:
    """When a types map is supplied and contains the type, sourceTypeId
    resolves to the type's display_name (human-readable)."""
    node = NodeDescriptor(
        node_id="ns=2;s=Boiler1",
        connection="conn_ref",
        display_name="Boiler1",
        node_class=NodeClass.Object,
        namespace_uri="urn:demo",
        type_source_id="ns=2;s=BoilerType",
        parent_node_id="ns=0;i=85",
        is_composition=True,
    )
    types = {
        "ns=2;s=BoilerType": TypeDescriptor(
            source_node_id="ns=2;s=BoilerType",
            display_name="BoilerType",
            namespace_uri="urn:demo",
            connection="conn_ref",
            structural_hash="abc",
            json_schema={"type": "object"},
        )
    }
    oi = to_object_instance(node, types=types)
    assert oi.metadata is not None
    assert oi.metadata.sourceTypeId == "BoilerType"


def test_object_instance_no_parent_emits_unknown_type_placeholder() -> None:
    node = NodeDescriptor(
        node_id="ns=0;i=85",
        connection="conn_ref",
        display_name="Objects",
        node_class=NodeClass.Object,
        namespace_uri="http://opcfoundation.org/UA/",
        type_source_id=None,
        parent_node_id=None,
        is_composition=True,
    )
    oi = to_object_instance(node)
    assert oi.parentId is None
    assert oi.typeElementId == "conn_ref!UnknownType"


def test_variable_instance_type_points_at_data_type() -> None:
    node = NodeDescriptor(
        node_id="ns=2;s=Boiler1/Temperature",
        connection="conn_ref",
        display_name="Temperature",
        node_class=NodeClass.Variable,
        namespace_uri="urn:demo",
        type_source_id="ns=0;i=11",
        parent_node_id="ns=2;s=Boiler1",
        is_composition=False,
    )
    oi = to_object_instance(node)
    assert oi.typeElementId == "conn_ref!ns=0;i=11"
    assert oi.isComposition is False
    assert oi.metadata is not None
    assert oi.metadata.system is not None
    assert oi.metadata.system["nodeClass"] == "Variable"


# ------------------------------------------------------------------ values


def _sample(quality: Quality = Quality.Good) -> ValueSample:
    return ValueSample(
        element_id="conn_ref!ns=2;s=Boiler1/Temperature",
        value={"Type": 11, "Body": 88.4},
        quality=quality,
        timestamp="2026-04-14T01:02:03Z",
    )


def test_current_value_maps_sample_to_vqt() -> None:
    result = to_current_value(_sample())
    assert result.value == 88.4
    assert result.quality == "Good"
    assert result.timestamp == "2026-04-14T01:02:03Z"
    assert result.isComposition is False
    assert result.components is None


def test_current_value_bad_quality() -> None:
    result = to_current_value(_sample(quality=Quality.Bad))
    assert result.quality == "Bad"


def test_composition_value_carries_components() -> None:
    comps = {
        "Temp": VQT(value=88.4, quality="Good", timestamp="2026-04-14T01:02:03Z"),
    }
    result = to_composition_value(
        components=comps, quality="Good", timestamp="2026-04-14T01:02:03Z"
    )
    assert result.isComposition is True
    assert result.value is None
    assert result.components is not None
    assert result.components["Temp"].value == 88.4


def test_empty_value() -> None:
    result = to_empty_value(timestamp="2026-04-14T01:02:03Z")
    assert result.isComposition is False
    assert result.quality == "GoodNoData"
    assert result.value == {}  # non-composition: empty dict, not null
    assert result.components is None


def test_historical_value_carries_vqt_list() -> None:
    s1 = ValueSample(
        element_id="conn_ref!ns=2;s=Boiler1/Temperature",
        value={"Type": 11, "Body": 80.1},
        quality=Quality.Good,
        timestamp="2026-04-14T00:00:00Z",
    )
    s2 = ValueSample(
        element_id="conn_ref!ns=2;s=Boiler1/Temperature",
        value={"Type": 11, "Body": 81.2},
        quality=Quality.Good,
        timestamp="2026-04-14T00:00:05Z",
    )
    hv = to_historical_value(s1.element_id, [s1, s2])
    assert len(hv.values) == 2
    assert hv.values[0].value == 80.1
    assert hv.values[1].timestamp == "2026-04-14T00:00:05Z"


# ------------------------------------------------------------------ status code -> quality


def test_quality_map_covers_canonical_symbols() -> None:
    assert QUALITY_SYMBOL_MAP["Good"] is Quality.Good
    assert QUALITY_SYMBOL_MAP["GoodNoData"] is Quality.GoodNoData


def test_bad_prefix_symbols_collapse_to_bad() -> None:
    assert status_symbol_to_quality("Bad_NoData") is Quality.Bad
    assert status_symbol_to_quality("Bad_AccessDenied") is Quality.Bad


def test_uncertain_symbols_map_to_goodnodata() -> None:
    assert status_symbol_to_quality("Uncertain") is Quality.GoodNoData
    assert status_symbol_to_quality("Uncertain_InitialValue") is Quality.GoodNoData


def test_unknown_symbol_defaults_to_goodnodata() -> None:
    assert status_symbol_to_quality("SomethingWeird") is Quality.GoodNoData
