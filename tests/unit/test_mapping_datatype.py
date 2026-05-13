"""to_object_instance populates dataType + dataTypeName for Variables."""

from i3xua.core.mapping import to_object_instance
from i3xua.core.neutral import NodeClass, NodeDescriptor, TypeDescriptor


def _variable(*, data_type_node_id: str | None) -> NodeDescriptor:
    return NodeDescriptor(
        node_id="ns=5;i=1242",
        connection="conn",
        display_name="Output",
        node_class=NodeClass.Variable,
        namespace_uri="http://example.org/UA/",
        type_source_id="i=2368",
        parent_node_id="ns=5;i=1241",
        is_composition=False,
        data_type_node_id=data_type_node_id,
    )


def _object() -> NodeDescriptor:
    return NodeDescriptor(
        node_id="ns=5;i=1241",
        connection="conn",
        display_name="FTX001",
        node_class=NodeClass.Object,
        namespace_uri="http://example.org/UA/",
        type_source_id="ns=5;i=1050",
        parent_node_id="ns=5;i=1240",
        is_composition=True,
    )


def test_variable_with_double_emits_data_type() -> None:
    inst = to_object_instance(_variable(data_type_node_id="i=11"))
    assert inst.metadata is not None
    assert inst.metadata.dataType == "i=11"
    assert inst.metadata.dataTypeName == "Double"


def test_variable_with_unknown_datatype_emits_nodeid_only() -> None:
    inst = to_object_instance(_variable(data_type_node_id="ns=2;i=3001"))
    assert inst.metadata is not None
    assert inst.metadata.dataType == "ns=2;i=3001"
    assert inst.metadata.dataTypeName is None


def test_variable_with_unknown_datatype_resolves_via_type_registry() -> None:
    custom_type = TypeDescriptor(
        source_node_id="ns=2;i=3001",
        display_name="MyCustomEnum",
        namespace_uri="http://example.org/UA/",
        connection="conn",
        structural_hash="abc",
        json_schema={"type": "object"},
    )
    inst = to_object_instance(
        _variable(data_type_node_id="ns=2;i=3001"),
        types={"ns=2;i=3001": custom_type},
    )
    assert inst.metadata is not None
    assert inst.metadata.dataType == "ns=2;i=3001"
    assert inst.metadata.dataTypeName == "MyCustomEnum"


def test_variable_with_no_datatype_omits_fields() -> None:
    inst = to_object_instance(_variable(data_type_node_id=None))
    assert inst.metadata is not None
    assert inst.metadata.dataType is None
    assert inst.metadata.dataTypeName is None


def test_object_omits_data_type_fields() -> None:
    inst = to_object_instance(_object())
    assert inst.metadata is not None
    dump = inst.metadata.model_dump(exclude_none=True)
    assert "dataType" not in dump
    assert "dataTypeName" not in dump
