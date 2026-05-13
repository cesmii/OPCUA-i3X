"""TypeInfo carries optional data_type_node_id for VariableTypes."""

from i3xua.adapters.asyncua.browse import TypeInfo


def test_typeinfo_default_data_type_is_none() -> None:
    ti = TypeInfo(
        node_id="i=58",
        display_name="BaseObjectType",
        namespace_uri="http://opcfoundation.org/UA/",
        fields=(),
    )
    assert ti.data_type_node_id is None


def test_typeinfo_carries_data_type_for_variabletype() -> None:
    ti = TypeInfo(
        node_id="i=2368",
        display_name="AnalogItemType",
        namespace_uri="http://opcfoundation.org/UA/",
        fields=(),
        data_type_node_id="i=11",
    )
    assert ti.data_type_node_id == "i=11"
