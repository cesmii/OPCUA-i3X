"""DataType -> JSON Schema."""

from __future__ import annotations

from i3xua.adapters.asyncua.typemap import (
    BUILTIN_SCHEMA,
    BUILTIN_VARIANT_TYPES,
    datatype_to_json_schema,
    enum_to_json_schema,
    structure_to_json_schema,
)
from i3xua.ouajson.types import VariantType


def test_builtin_table_is_exhaustive_over_variant_types() -> None:
    # Every entry from Boolean..DiagnosticInfo (1..25) must be mapped.
    covered = set(BUILTIN_VARIANT_TYPES.values())
    for vt in VariantType:
        if vt is VariantType.Null:
            continue
        assert vt in covered, f"missing builtin for {vt!r}"


def test_int32_schema_has_bounds() -> None:
    assert datatype_to_json_schema("ns=0;i=6") == {
        "type": "integer",
        "minimum": -2147483648,
        "maximum": 2147483647,
    }


def test_int64_schema_is_string_pattern() -> None:
    schema = datatype_to_json_schema("ns=0;i=8")
    assert schema["type"] == "string"
    assert schema["pattern"].startswith("^-?")


def test_array_wrapping_applied_when_value_rank_at_least_one() -> None:
    schema = datatype_to_json_schema("ns=0;i=11", value_rank=1)
    assert schema == {"type": "array", "items": {"type": "number"}}


def test_abstract_number_resolves_to_double() -> None:
    assert datatype_to_json_schema("ns=0;i=26") == BUILTIN_SCHEMA["ns=0;i=11"]


def test_unknown_datatype_falls_through_resolver_then_fallback() -> None:
    called: list[str] = []

    def resolver(nid: str) -> dict | None:
        called.append(nid)
        return None

    schema = datatype_to_json_schema("ns=2;s=Custom", resolver=resolver)
    assert called == ["ns=2;s=Custom"]
    assert schema["type"] == "object"


def test_resolver_hit_is_used_verbatim() -> None:
    def resolver(nid: str) -> dict | None:
        return {"type": "string", "description": "custom"}

    schema = datatype_to_json_schema("ns=2;s=Weird", resolver=resolver)
    assert schema == {"type": "string", "description": "custom"}


def test_structure_schema_collects_required_fields() -> None:
    fields = [
        ("Temp", "ns=0;i=11", -1, False),
        ("Pressure", "ns=0;i=11", -1, True),  # optional
        ("Labels", "ns=0;i=12", 1, False),  # array of String
    ]
    schema = structure_to_json_schema(fields)
    assert schema["type"] == "object"
    assert schema["required"] == ["Temp", "Labels"]
    assert schema["properties"]["Labels"] == {
        "type": "array",
        "items": {"type": "string"},
    }


def test_structure_schema_with_nested_struct_via_resolver() -> None:
    def resolver(nid: str) -> dict | None:
        if nid == "ns=2;s=Inner":
            return {"type": "object", "properties": {"x": {"type": "integer"}}}
        return None

    schema = structure_to_json_schema([("inner", "ns=2;s=Inner", -1, False)], resolver=resolver)
    assert schema["properties"]["inner"]["properties"]["x"]["type"] == "integer"


def test_unresolved_tracker_normalizes_bare_ns0_form() -> None:
    """asyncua's NodeId.to_string() emits bare `i=N` for ns=0, but BUILTIN_SCHEMA
    keys use `ns=0;i=N`. structure_to_json_schema_with_unresolved must normalize
    before checking — otherwise standard builtins would be flagged as unresolved."""
    from i3xua.adapters.asyncua.typemap import structure_to_json_schema_with_unresolved

    fields = [
        ("Temp", "i=11", -1, False),  # bare form for Double
        ("Name", "i=12", -1, False),  # bare form for String
        ("Custom", "ns=2;i=3001", -1, False),  # genuinely unresolved
    ]
    _, unresolved = structure_to_json_schema_with_unresolved(fields)
    assert unresolved == ("Custom",), (
        f"only the custom field should be unresolved; got {unresolved}"
    )


def test_enum_schema_carries_names_sidecar() -> None:
    schema = enum_to_json_schema([("Off", 0), ("Running", 1), ("Fault", 2)])
    assert schema == {
        "type": "integer",
        "enum": [0, 1, 2],
        "x-enum-names": ["Off", "Running", "Fault"],
    }
