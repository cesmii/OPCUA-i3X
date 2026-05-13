"""_browse_types injects a `value` property when TypeInfo carries data_type_node_id."""

from __future__ import annotations

import asyncio

from i3xua.adapters.asyncua.browse import (
    InstanceDeclaration,
    TypeInfo,
    _browse_types,
)


class _StubSource:
    def __init__(self, types: dict[str, TypeInfo]) -> None:
        self._types = types

    async def get_type_info(self, tid: str) -> TypeInfo | None:
        return self._types.get(tid)


def test_variabletype_schema_includes_value_with_datatype() -> None:
    src = _StubSource(
        {
            "i=2368": TypeInfo(
                node_id="i=2368",
                display_name="AnalogItemType",
                namespace_uri="http://opcfoundation.org/UA/",
                fields=(
                    InstanceDeclaration(
                        name="EURange", data_type_node_id="i=884", is_optional=False
                    ),
                ),
                data_type_node_id="i=11",
            ),
        }
    )
    out = asyncio.run(_browse_types(src, connection="conn", roots=("i=2368",)))
    assert len(out) == 1
    schema = out[0].json_schema
    props = schema["properties"]
    assert "value" in props
    assert props["value"] == {"type": "number"}
    assert "EURange" in props


def test_objecttype_schema_omits_value() -> None:
    src = _StubSource(
        {
            "i=58": TypeInfo(
                node_id="i=58",
                display_name="BaseObjectType",
                namespace_uri="http://opcfoundation.org/UA/",
                fields=(),
                data_type_node_id=None,
            ),
        }
    )
    out = asyncio.run(_browse_types(src, connection="conn", roots=("i=58",)))
    assert "value" not in out[0].json_schema.get("properties", {})


def test_variabletype_with_no_fields_still_emits_value() -> None:
    """BaseDataVariableType has no Mandatory/Optional declarations but should
    still surface its DataType via `value`."""
    src = _StubSource(
        {
            "i=63": TypeInfo(
                node_id="i=63",
                display_name="BaseDataVariableType",
                namespace_uri="http://opcfoundation.org/UA/",
                fields=(),
                data_type_node_id="i=24",  # BaseDataType
            ),
        }
    )
    out = asyncio.run(_browse_types(src, connection="conn", roots=("i=63",)))
    schema = out[0].json_schema
    assert schema.get("properties", {}).get("value") is not None
