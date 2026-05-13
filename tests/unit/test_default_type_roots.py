"""DEFAULT_TYPE_ROOTS includes both BaseObjectType and BaseVariableType."""

from i3xua.adapters.asyncua.browse import DEFAULT_TYPE_ROOTS


def test_default_type_roots_includes_base_object_type() -> None:
    assert "i=58" in DEFAULT_TYPE_ROOTS or "ns=0;i=58" in DEFAULT_TYPE_ROOTS


def test_default_type_roots_includes_base_variable_type() -> None:
    assert "i=62" in DEFAULT_TYPE_ROOTS or "ns=0;i=62" in DEFAULT_TYPE_ROOTS
