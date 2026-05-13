"""OPC UA DataType NodeId -> JSON Schema.

Builtin DataTypes (namespace 0, ids 1..25) come from a static table. Custom
Structures/Enums loaded via `load_data_type_definitions` are resolved via an
injectable `resolver` callback so this module stays decoupled from asyncua.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from i3xua.ouajson.types import VariantType

# ------------------------------------------------------------------ tables

BUILTIN_VARIANT_TYPES: dict[str, VariantType] = {
    "ns=0;i=1": VariantType.Boolean,
    "ns=0;i=2": VariantType.SByte,
    "ns=0;i=3": VariantType.Byte,
    "ns=0;i=4": VariantType.Int16,
    "ns=0;i=5": VariantType.UInt16,
    "ns=0;i=6": VariantType.Int32,
    "ns=0;i=7": VariantType.UInt32,
    "ns=0;i=8": VariantType.Int64,
    "ns=0;i=9": VariantType.UInt64,
    "ns=0;i=10": VariantType.Float,
    "ns=0;i=11": VariantType.Double,
    "ns=0;i=12": VariantType.String,
    "ns=0;i=13": VariantType.DateTime,
    "ns=0;i=14": VariantType.Guid,
    "ns=0;i=15": VariantType.ByteString,
    "ns=0;i=16": VariantType.XmlElement,
    "ns=0;i=17": VariantType.NodeId,
    "ns=0;i=18": VariantType.ExpandedNodeId,
    "ns=0;i=19": VariantType.StatusCode,
    "ns=0;i=20": VariantType.QualifiedName,
    "ns=0;i=21": VariantType.LocalizedText,
    "ns=0;i=22": VariantType.ExtensionObject,  # Structure base
    "ns=0;i=23": VariantType.DataValue,
    "ns=0;i=24": VariantType.Variant,
    "ns=0;i=25": VariantType.DiagnosticInfo,
}

BUILTIN_SCHEMA: dict[str, dict[str, Any]] = {
    "ns=0;i=1": {"type": "boolean"},
    "ns=0;i=2": {"type": "integer", "minimum": -128, "maximum": 127},
    "ns=0;i=3": {"type": "integer", "minimum": 0, "maximum": 255},
    "ns=0;i=4": {"type": "integer", "minimum": -32768, "maximum": 32767},
    "ns=0;i=5": {"type": "integer", "minimum": 0, "maximum": 65535},
    "ns=0;i=6": {"type": "integer", "minimum": -2147483648, "maximum": 2147483647},
    "ns=0;i=7": {"type": "integer", "minimum": 0, "maximum": 4294967295},
    # Int64 / UInt64 travel as JSON strings per Part 6 §5.4.2.
    "ns=0;i=8": {"type": "string", "pattern": "^-?[0-9]+$"},
    "ns=0;i=9": {"type": "string", "pattern": "^[0-9]+$"},
    "ns=0;i=10": {"type": "number"},
    "ns=0;i=11": {"type": "number"},
    "ns=0;i=12": {"type": "string"},
    "ns=0;i=13": {"type": "string", "format": "date-time"},
    "ns=0;i=14": {"type": "string", "format": "uuid"},
    "ns=0;i=15": {"type": "string", "contentEncoding": "base64"},
    "ns=0;i=16": {"type": "string"},
    "ns=0;i=17": {"type": "object", "description": "OPC UA NodeId (Part 6 §5.4.2.10)"},
    "ns=0;i=18": {"type": "object", "description": "OPC UA ExpandedNodeId"},
    "ns=0;i=19": {"type": "object", "description": "OPC UA StatusCode"},
    "ns=0;i=20": {"type": "object", "description": "OPC UA QualifiedName"},
    "ns=0;i=21": {"type": "object", "description": "OPC UA LocalizedText"},
    "ns=0;i=22": {"type": "object", "description": "OPC UA Structure (base)"},
    "ns=0;i=23": {"type": "object", "description": "OPC UA DataValue"},
    "ns=0;i=24": {"type": "object", "description": "OPC UA Variant"},
    "ns=0;i=25": {"type": "object", "description": "OPC UA DiagnosticInfo"},
}

# Friendly aliases used by BaseDataType subtypes (Number/Integer/UInteger/Enumeration).
# These default to Double/Int64/UInt64/Int32 respectively so consumers still get a
# JSON Schema they can validate against.
ABSTRACT_ALIAS: dict[str, str] = {
    "ns=0;i=26": "ns=0;i=11",  # Number -> Double
    "ns=0;i=27": "ns=0;i=8",  # Integer -> Int64
    "ns=0;i=28": "ns=0;i=9",  # UInteger -> UInt64
    "ns=0;i=29": "ns=0;i=6",  # Enumeration -> Int32
}


Resolver = Callable[[str], dict[str, Any] | None]
"""Given a custom DataType NodeId, return its JSON Schema or None."""


def _fallback() -> dict[str, Any]:
    return {"type": "object", "description": "Unknown DataType (fallback)"}


def _ns0_normalize(node_id: str) -> str:
    """Return the ns=0 form for bare 'i=N' NodeIds; leave others unchanged.

    asyncua's NodeId.to_string() strips 'ns=0;' for namespace-0 nodes.
    BUILTIN_SCHEMA and ABSTRACT_ALIAS use the 'ns=0;i=N' form, so callers
    passing either form both resolve correctly.
    """
    if (
        node_id.startswith("i=")
        or node_id.startswith("g=")
        or node_id.startswith("b=")
        or node_id.startswith("s=")
    ):
        return f"ns=0;{node_id}"
    return node_id


def datatype_to_json_schema(
    node_id: str,
    *,
    value_rank: int = -1,
    is_optional: bool = False,
    resolver: Resolver | None = None,
) -> dict[str, Any]:
    """Resolve `node_id` to a JSON Schema, optionally wrapping for arrays."""
    node_id = _ns0_normalize(node_id)
    canonical = ABSTRACT_ALIAS.get(node_id, node_id)
    schema = BUILTIN_SCHEMA.get(canonical)
    if schema is None and resolver is not None:
        schema = resolver(canonical)
    if schema is None:
        schema = _fallback()
    if value_rank >= 1:
        schema = {"type": "array", "items": schema}
    return schema


def structure_to_json_schema(
    fields: list[tuple[str, str, int, bool]],
    *,
    resolver: Resolver | None = None,
) -> dict[str, Any]:
    """Build a JSON Schema 'object' from a list of (name, dataTypeNodeId, valueRank, isOptional)."""
    schema, _unresolved = structure_to_json_schema_with_unresolved(fields, resolver=resolver)
    return schema


def structure_to_json_schema_with_unresolved(
    fields: list[tuple[str, str, int, bool]],
    *,
    resolver: Resolver | None = None,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """like `structure_to_json_schema` but also returns the tuple of
    field names whose DataType couldn't be concretely resolved (hit the
    `Unknown DataType (fallback)` path or an abstract base's coarse fallback).
    Instances of a type with unresolved fields surface them via
    `extendedAttributes` on the instance side.
    """
    properties: dict[str, Any] = {}
    required: list[str] = []
    unresolved: list[str] = []
    for name, dt, rank, optional in fields:
        # Normalize bare `i=N` (asyncua's NodeId.to_string() form for ns=0) to
        # `ns=0;i=N` so the BUILTIN_SCHEMA / ABSTRACT_ALIAS lookups hit. Without
        # this, fields whose DataType comes through bare were incorrectly flagged
        # as unresolved even when they were standard builtins.
        normalized = _ns0_normalize(dt)
        canonical = ABSTRACT_ALIAS.get(normalized, normalized)
        known_builtin = canonical in BUILTIN_SCHEMA
        resolver_hit = resolver(canonical) is not None if resolver is not None else False
        schema_fragment = datatype_to_json_schema(
            dt, value_rank=rank, is_optional=optional, resolver=resolver
        )
        properties[name] = schema_fragment
        if not optional:
            required.append(name)
        if not known_builtin and not resolver_hit:
            unresolved.append(name)
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema, tuple(unresolved)


def enum_to_json_schema(values: list[tuple[str, int]]) -> dict[str, Any]:
    """Build a JSON Schema for an OPC UA Enumeration given [(name, value), ...]."""
    return {
        "type": "integer",
        "enum": [v for _, v in values],
        "x-enum-names": [n for n, _ in values],
    }
