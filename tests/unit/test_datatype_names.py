"""Built-in DataType NodeId -> name lookup."""

from i3xua.core.datatype_names import BUILTIN_DATATYPE_NAMES, lookup_datatype_name


def test_lookup_double() -> None:
    assert lookup_datatype_name("i=11") == "Double"


def test_lookup_string() -> None:
    assert lookup_datatype_name("i=12") == "String"


def test_lookup_boolean() -> None:
    assert lookup_datatype_name("i=1") == "Boolean"


def test_lookup_int32() -> None:
    assert lookup_datatype_name("i=6") == "Int32"


def test_lookup_unknown_returns_none() -> None:
    assert lookup_datatype_name("ns=2;i=3001") is None


def test_table_canonical_form_only() -> None:
    """Every key in the table is in canonical (no 'ns=0;') form."""
    for key in BUILTIN_DATATYPE_NAMES:
        assert not key.startswith("ns=0;"), f"non-canonical key in table: {key!r}"
