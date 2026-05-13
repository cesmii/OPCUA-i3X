"""Neutral OPC UA type dataclasses consumed by the Part-6 JSON codec.

Defined here (rather than imported from asyncua) so the codec has no dependency
on the adapter layer. The asyncua adapter translates `ua.Variant` / `ua.DataValue`
into these shapes before handing them to `ouajson.encode`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum
from typing import Any
from uuid import UUID


class VariantType(IntEnum):
    """OPC UA Part 6 builtin type IDs (1..25). Null/invalid = 0."""

    Null = 0
    Boolean = 1
    SByte = 2
    Byte = 3
    Int16 = 4
    UInt16 = 5
    Int32 = 6
    UInt32 = 7
    Int64 = 8
    UInt64 = 9
    Float = 10
    Double = 11
    String = 12
    DateTime = 13
    Guid = 14
    ByteString = 15
    XmlElement = 16
    NodeId = 17
    ExpandedNodeId = 18
    StatusCode = 19
    QualifiedName = 20
    LocalizedText = 21
    ExtensionObject = 22
    DataValue = 23
    Variant = 24
    DiagnosticInfo = 25


class NodeIdType(IntEnum):
    Numeric = 0
    String = 1
    Guid = 2
    Opaque = 3  # ByteString


@dataclass(frozen=True, slots=True)
class NodeId:
    id: int | str | UUID | bytes
    namespace: int = 0
    id_type: NodeIdType | None = None

    def resolved_type(self) -> NodeIdType:
        if self.id_type is not None:
            return self.id_type
        if isinstance(self.id, bool) or not isinstance(self.id, int | str | UUID | bytes):
            raise TypeError(f"unsupported NodeId identifier type: {type(self.id).__name__}")
        if isinstance(self.id, int):
            return NodeIdType.Numeric
        if isinstance(self.id, str):
            return NodeIdType.String
        if isinstance(self.id, UUID):
            return NodeIdType.Guid
        return NodeIdType.Opaque


@dataclass(frozen=True, slots=True)
class ExpandedNodeId:
    node_id: NodeId
    namespace_uri: str | None = None
    server_index: int = 0


@dataclass(frozen=True, slots=True)
class QualifiedName:
    name: str
    namespace_index: int = 0


@dataclass(frozen=True, slots=True)
class LocalizedText:
    text: str | None = None
    locale: str | None = None


@dataclass(frozen=True, slots=True)
class StatusCode:
    code: int = 0  # uint32 raw status code
    symbol: str = "Good"  # "Good" | "Uncertain" | "Bad" | "Bad_<reason>"


GOOD_STATUS = StatusCode(code=0, symbol="Good")


@dataclass(frozen=True, slots=True)
class Variant:
    variant_type: VariantType
    body: Any  # scalar | list | nested lists (for matrix)
    dimensions: tuple[int, ...] | None = None  # present for ValueRank > 1


@dataclass(frozen=True, slots=True)
class StructureField:
    name: str
    data_type_node_id: str
    value_rank: int = -1
    is_optional: bool = False


@dataclass(frozen=True, slots=True)
class StructureDefinition:
    type_id: NodeId
    fields: tuple[StructureField, ...]


@dataclass(frozen=True, slots=True)
class ExtensionObject:
    """A structured value whose wire shape is determined by `definition`.

    `fields` is a dict keyed by field name; values are Python-native (int, str,
    datetime, nested ExtensionObject, …). The encoder walks `definition.fields`
    in declaration order to produce `{TypeId, Body}`.
    """

    definition: StructureDefinition
    fields: dict[str, Any]


@dataclass(frozen=True, slots=True)
class DataValue:
    value: Variant
    status: StatusCode = field(default_factory=lambda: GOOD_STATUS)
    source_timestamp: datetime | None = None
    source_picoseconds: int = 0
    server_timestamp: datetime | None = None
    server_picoseconds: int = 0
