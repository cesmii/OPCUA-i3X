"""TypeDescriptor.source_node_id is canonical (no `ns=0;` prefix)."""

from __future__ import annotations

import asyncio

from i3xua.adapters.asyncua.browse import (
    TypeInfo,
    _browse_types,
)


class _StubSource:
    def __init__(self, types: dict[str, TypeInfo]) -> None:
        self._types = types

    async def get_type_info(self, tid: str) -> TypeInfo | None:
        return self._types.get(tid)


def test_type_descriptor_source_node_id_is_canonical() -> None:
    """A type seeded as `ns=0;i=58` must emit as `i=58` in TypeDescriptor."""
    src = _StubSource(
        {
            "ns=0;i=58": TypeInfo(
                node_id="ns=0;i=58",
                display_name="BaseObjectType",
                namespace_uri="http://opcfoundation.org/UA/",
                fields=(),
            ),
        }
    )
    out = asyncio.run(_browse_types(src, connection="conn", roots=("ns=0;i=58",)))
    assert len(out) == 1
    assert out[0].source_node_id == "i=58"


def test_type_descriptor_non_zero_namespace_kept() -> None:
    """`ns=2;...` IDs are not stripped — only `ns=0;` is."""
    src = _StubSource(
        {
            "ns=2;i=1050": TypeInfo(
                node_id="ns=2;i=1050",
                display_name="VendorType",
                namespace_uri="http://example.org/",
                fields=(),
            ),
        }
    )
    out = asyncio.run(_browse_types(src, connection="conn", roots=("ns=2;i=1050",)))
    assert out[0].source_node_id == "ns=2;i=1050"
