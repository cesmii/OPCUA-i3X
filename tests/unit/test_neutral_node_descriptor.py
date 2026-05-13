"""Smoke test for the NodeDescriptor field shape."""

from i3xua.core.neutral import NodeClass, NodeDescriptor


def test_node_descriptor_carries_data_type_node_id_for_variables() -> None:
    nd = NodeDescriptor(
        node_id="ns=5;i=1242",
        connection="conn",
        display_name="Output",
        node_class=NodeClass.Variable,
        namespace_uri="http://example.org/UA/",
        type_source_id="i=2368",  # AnalogItemType TypeDefinition
        parent_node_id="ns=5;i=1241",
        is_composition=False,
        data_type_node_id="i=11",  # Double
    )
    assert nd.data_type_node_id == "i=11"
    assert nd.type_source_id == "i=2368"


def test_node_descriptor_data_type_defaults_none() -> None:
    nd = NodeDescriptor(
        node_id="ns=5;i=1241",
        connection="conn",
        display_name="FTX001",
        node_class=NodeClass.Object,
        namespace_uri="http://example.org/UA/",
        type_source_id="ns=5;i=1050",
        parent_node_id="ns=5;i=1240",
        is_composition=True,
    )
    assert nd.data_type_node_id is None


def test_node_descriptor_carries_variable_attributes() -> None:
    """D-66/D-67 — six Variable attributes batched in a single Read."""
    nd = NodeDescriptor(
        node_id="ns=2;s=Tag1",
        connection="kepware",
        display_name="Tag1",
        node_class=NodeClass.Variable,
        namespace_uri="urn:demo",
        type_source_id="i=63",
        parent_node_id="ns=2;s=Channel1",
        is_composition=False,
        data_type_node_id="i=11",
        access_level=1,
        user_access_level=1,
        value_rank=-1,
        array_dimensions=None,
        historizing=False,
        minimum_sampling_interval=10.0,
    )
    assert nd.access_level == 1
    assert nd.user_access_level == 1
    assert nd.value_rank == -1
    assert nd.array_dimensions is None
    assert nd.historizing is False
    assert nd.minimum_sampling_interval == 10.0


def test_node_descriptor_variable_attribute_defaults_none() -> None:
    """Non-Variable nodes (and Variables not yet attribute-read) leave
    every attribute None — the mapping layer drops absent fields via
    Pydantic exclude_none."""
    nd = NodeDescriptor(
        node_id="ns=0;i=85",
        connection="conn",
        display_name="Objects",
        node_class=NodeClass.Object,
        namespace_uri="urn:demo",
        type_source_id=None,
        parent_node_id=None,
        is_composition=True,
    )
    assert nd.access_level is None
    assert nd.user_access_level is None
    assert nd.value_rank is None
    assert nd.array_dimensions is None
    assert nd.historizing is None
    assert nd.minimum_sampling_interval is None
