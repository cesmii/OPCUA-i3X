"""Wrapper-synthesized 'artificial' ObjectTypes.

When the OPC UA server reports a Variable's TypeDefinition as
``BaseDataVariableType`` (``i=63``) AND the parent has no meaningful type,
the declared type is uninformative — tag-flat namespaces would collapse
to a single 'BaseDataVariableType' folder in the client. This module
derives a more meaningful shape from the Variable's attributes
(DataType × ValueRank × AccessLevel) and registers a synthetic ObjectType
for each observed shape under the same server namespace the source
Variables live in (``<conn>!<shape>`` elementIds, co-located with
instances).

Pure data — no asyncua, no I/O. Derivation is deterministic; the same
triple yields the same TypeDescriptor across browse cycles, so the
TypeRegistry's snapshot-swap is a no-op when the shape set is unchanged.
"""

from __future__ import annotations

from i3xua.core.datatype_names import lookup_datatype_name

# Server-reported TypeDefinitions we replace with the artificial form.
# Only ``BaseDataVariableType`` (``i=63``) — tag-heavy servers tag every
# Variable with this when there's no better type. ``PropertyType``
# (``i=68``) is intentionally NOT here: a PropertyType-typed Variable is
# structurally significant (it's a Property of its typed parent — EURange
# on AnalogItemType, TrueState on TwoStateVariable) and the parent's type
# already carries the semantic context. Swapping ``i=68`` hides that
# relationship and pollutes the artificial-types catalog.
GENERIC_VARIABLE_TYPES: frozenset[str] = frozenset({"i=63"})


def derive_shape(
    data_type_node_id: str,
    value_rank: int,
    access_level: int,
    *,
    dt_name_override: str | None = None,
) -> tuple[str, str, str]:
    """Compute the (DataType, Rank, Access) triple naming an artificial type.

    DataType: ``dt_name_override`` when supplied (browse-time-resolved
    BrowseName for non-Part-6 types like Duration, UtcTime, Decimal),
    else Part 6 builtin name via ``lookup_datatype_name``, else
    ``Custom_<sanitized-NodeId>`` so vendor types still get a stable,
    NodeId-derived label.

    Rank: ``Scalar`` (-1), ``AnyDim`` (0), ``Array`` (1), ``Multi`` (2+).

    Access: ``RO`` / ``WO`` / ``RW`` / ``NONE`` from CurrentRead/CurrentWrite
    bits. Higher AccessLevel bits are ignored to bound combinatorics; they're
    tracked separately on ``metadata.system.historizing`` where it matters.
    """
    dt_name = dt_name_override or lookup_datatype_name(data_type_node_id)
    if dt_name is None:
        sanitized = data_type_node_id.replace("=", "").replace(";", "_").replace(":", "_")
        dt_name = f"Custom_{sanitized}"

    if value_rank == -1:
        rank = "Scalar"
    elif value_rank == 0:
        rank = "AnyDim"
    elif value_rank == 1:
        rank = "Array"
    else:
        rank = "Multi"

    has_read = bool(access_level & 0b01)
    has_write = bool(access_level & 0b10)
    if has_read and has_write:
        access = "RW"
    elif has_read:
        access = "RO"
    elif has_write:
        access = "WO"
    else:
        access = "NONE"

    return (dt_name, rank, access)


def artificial_type_name(shape: tuple[str, str, str]) -> str:
    """`<DataType>_<Rank>_<Access>` — stable elementId-suffix form."""
    return f"{shape[0]}_{shape[1]}_{shape[2]}"


def should_replace(server_type_id: str | None) -> bool:
    """True when the server-reported TypeDefinition is generic enough to override."""
    if server_type_id is None:
        return False
    return server_type_id in GENERIC_VARIABLE_TYPES


__all__ = [
    "GENERIC_VARIABLE_TYPES",
    "artificial_type_name",
    "derive_shape",
    "should_replace",
]
