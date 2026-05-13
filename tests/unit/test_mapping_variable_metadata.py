"""D-67 — `to_object_instance` surfaces Variable attributes on `metadata.system`."""

from __future__ import annotations

from typing import Any

from i3xua.core.mapping import to_object_instance
from i3xua.core.neutral import NodeClass, NodeDescriptor


def _variable(**overrides: Any) -> NodeDescriptor:
    base: dict[str, Any] = dict(
        node_id="ns=2;s=Tag1",
        connection="kepware",
        display_name="Tag1",
        node_class=NodeClass.Variable,
        namespace_uri="urn:demo",
        type_source_id="i=63",
        parent_node_id="ns=2;s=Channel1",
        is_composition=False,
        data_type_node_id="i=11",
        access_level=3,
        user_access_level=3,
        value_rank=-1,
        array_dimensions=None,
        historizing=False,
        minimum_sampling_interval=10.0,
    )
    base.update(overrides)
    return NodeDescriptor(**base)


def _object() -> NodeDescriptor:
    return NodeDescriptor(
        node_id="ns=2;s=Channel1",
        connection="kepware",
        display_name="Channel1",
        node_class=NodeClass.Object,
        namespace_uri="urn:demo",
        type_source_id="i=58",
        parent_node_id=None,
        is_composition=True,
    )


def test_variable_metadata_appears_on_metadata_system() -> None:
    inst = to_object_instance(_variable())
    assert inst.metadata is not None and inst.metadata.system is not None
    sys = inst.metadata.system
    assert sys.get("accessLevel") == "rw"
    assert sys.get("userAccessLevel") == "rw"
    assert sys.get("valueRank") == -1
    assert sys.get("historizing") is False
    assert sys.get("minimumSamplingInterval") == 10.0
    assert "arrayDimensions" not in sys


def test_access_level_string_for_read_only() -> None:
    inst = to_object_instance(_variable(access_level=1, user_access_level=1))
    assert inst.metadata is not None and inst.metadata.system is not None
    assert inst.metadata.system["accessLevel"] == "r"


def test_access_level_string_for_write_only() -> None:
    inst = to_object_instance(_variable(access_level=2, user_access_level=2))
    assert inst.metadata is not None and inst.metadata.system is not None
    assert inst.metadata.system["accessLevel"] == "w"


def test_access_level_string_for_no_access() -> None:
    inst = to_object_instance(_variable(access_level=0, user_access_level=0))
    assert inst.metadata is not None and inst.metadata.system is not None
    assert inst.metadata.system["accessLevel"] == "none"


def test_object_omits_variable_attributes_in_system() -> None:
    inst = to_object_instance(_object())
    assert inst.metadata is not None and inst.metadata.system is not None
    sys = inst.metadata.system
    assert "accessLevel" not in sys
    assert "valueRank" not in sys
    assert "historizing" not in sys


def test_variable_with_array_dimensions_emits_them() -> None:
    inst = to_object_instance(_variable(value_rank=1, array_dimensions=(8,)))
    assert inst.metadata is not None and inst.metadata.system is not None
    assert inst.metadata.system["valueRank"] == 1
    assert inst.metadata.system["arrayDimensions"] == [8]


def test_variable_with_no_attributes_emits_minimal_system() -> None:
    """Variables without any attributes (e.g. a node we couldn't read)
    still carry the always-present nodeClass + sourceNodeId."""
    nd = NodeDescriptor(
        node_id="ns=2;s=Tag2",
        connection="kepware",
        display_name="Tag2",
        node_class=NodeClass.Variable,
        namespace_uri="urn:demo",
        type_source_id="i=63",
        parent_node_id=None,
        is_composition=False,
    )
    inst = to_object_instance(nd)
    assert inst.metadata is not None and inst.metadata.system is not None
    sys = inst.metadata.system
    assert sys["nodeClass"] == "Variable"
    assert sys["sourceNodeId"] == "ns=2;s=Tag2"
    for missing in (
        "accessLevel",
        "userAccessLevel",
        "valueRank",
        "arrayDimensions",
        "historizing",
        "minimumSamplingInterval",
    ):
        assert missing not in sys
