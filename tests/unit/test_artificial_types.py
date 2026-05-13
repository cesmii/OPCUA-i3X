"""D-69..D-73 — pure-data artificial-type derivation."""

from __future__ import annotations

from i3xua.core.artificial_types import (
    GENERIC_VARIABLE_TYPES,
    artificial_type_name,
    derive_shape,
    should_replace,
)


def test_generic_types_set_covers_basedatavariabletype_only() -> None:
    """Only `BaseDataVariableType` (`i=63`) is replaced. PropertyType
    (`i=68`) is intentionally excluded — it's structurally meaningful (a
    Property of its typed parent) and the parent's type carries context.
    AnalogItemType etc. also pass through (server-reported type is meaningful)."""
    assert "i=63" in GENERIC_VARIABLE_TYPES
    assert "i=68" not in GENERIC_VARIABLE_TYPES
    assert "i=2368" not in GENERIC_VARIABLE_TYPES


def test_derive_shape_double_scalar_ro() -> None:
    assert derive_shape("i=11", -1, 0b001) == ("Double", "Scalar", "RO")


def test_derive_shape_uint32_array_rw() -> None:
    assert derive_shape("i=7", 1, 0b011) == ("UInt32", "Array", "RW")


def test_derive_shape_string_scalar_no_access() -> None:
    assert derive_shape("i=12", -1, 0b000) == ("String", "Scalar", "NONE")


def test_derive_shape_boolean_scalar_write_only() -> None:
    assert derive_shape("i=1", -1, 0b010) == ("Boolean", "Scalar", "WO")


def test_derive_shape_multi_dim_array() -> None:
    assert derive_shape("i=11", 2, 0b011) == ("Double", "Multi", "RW")


def test_derive_shape_any_dim() -> None:
    assert derive_shape("i=11", 0, 0b011) == ("Double", "AnyDim", "RW")


def test_derive_shape_custom_ns0_dtype() -> None:
    """Non-builtin ns=0 DataTypes get a `Custom_iNNN` label."""
    # Use a NodeId guaranteed not in BUILTIN_DATATYPE_NAMES (the well-known
    # Part-3/5 entries cover many "low" numeric IDs, so pick one above the
    # standard range).
    assert derive_shape("i=99999", -1, 0b001) == ("Custom_i99999", "Scalar", "RO")


def test_derive_shape_custom_non_ns0_dtype() -> None:
    """Vendor DataTypes get a `Custom_nsN_iMMM` label so collisions are
    impossible (NodeId is unique)."""
    assert derive_shape("ns=2;i=3001", -1, 0b001) == ("Custom_ns2_i3001", "Scalar", "RO")


def test_derive_shape_higher_access_bits_ignored() -> None:
    """HistoricalRead/Write bits don't expand the AccessLevel combinatorics —
    just CurrentRead (bit 0) and CurrentWrite (bit 1) drive the label."""
    assert derive_shape("i=11", -1, 0b1111) == ("Double", "Scalar", "RW")


def test_artificial_type_name_format() -> None:
    assert artificial_type_name(("UInt32", "Scalar", "RO")) == "UInt32_Scalar_RO"


def test_should_replace_for_basedatavariabletype() -> None:
    assert should_replace("i=63") is True


def test_should_replace_for_propertytype_passes_through() -> None:
    """PropertyType is structurally meaningful (parent's type carries the
    context); leave its `<conn>!i=68` typeElementId alone."""
    assert should_replace("i=68") is False


def test_should_replace_for_analogitemtype() -> None:
    assert should_replace("i=2368") is False


def test_should_replace_for_unknown_type_string() -> None:
    """An unrecognized type string isn't in GENERIC_VARIABLE_TYPES → no replace."""
    assert should_replace("UnknownType") is False


def test_should_replace_for_none() -> None:
    """`None` server type means "we couldn't resolve a TypeDefinition" — fall
    through to UnknownType placeholder per CESMII RFC §3.3, no artificial
    swap."""
    assert should_replace(None) is False
