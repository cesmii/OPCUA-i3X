"""asyncua native types -> neutral ouajson types.

This module is the narrow seam between `asyncua.ua` and the rest of the code.
Keeping the translation here lets the test suite inject a handful of fake
asyncua-shaped objects (duck-typed) without importing the real library.
"""

from __future__ import annotations

import base64
from typing import Any

from i3xua.core.neutral import Quality
from i3xua.ouajson.encode import encode_node_id
from i3xua.ouajson.types import (
    DataValue,
    ExpandedNodeId,
    ExtensionObject,
    LocalizedText,
    NodeId,
    NodeIdType,
    QualifiedName,
    StatusCode,
    StructureDefinition,
    StructureField,
    Variant,
    VariantType,
)

# asyncua's `ua.NodeIdType` is a 6-value enum (TwoByte=0, FourByte=1, Numeric=2,
# String=3, Guid=4, ByteString=5) — TwoByte/FourByte/Numeric are all wire-encoding
# variants of a numeric identifier. Part 6 collapses those into one IdType
# (Numeric). Map asyncua's enum values onto our 4-value `NodeIdType`.
_NODEID_TYPES = {
    0: NodeIdType.Numeric,  # TwoByte (identifier encoded as 0..255)
    1: NodeIdType.Numeric,  # FourByte (identifier encoded as 0..65535, namespace 0..255)
    2: NodeIdType.Numeric,  # Numeric (full 32-bit namespace + 32-bit identifier)
    3: NodeIdType.String,
    4: NodeIdType.Guid,
    5: NodeIdType.Opaque,  # ByteString
}


def from_ua_node_id(node_id: Any) -> NodeId:
    return NodeId(
        id=node_id.Identifier,
        namespace=int(node_id.NamespaceIndex),
        id_type=_NODEID_TYPES.get(int(node_id.NodeIdType)),
    )


def from_ua_expanded_node_id(eid: Any) -> ExpandedNodeId:
    inner = from_ua_node_id(eid)
    uri = getattr(eid, "NamespaceUri", None)
    server = int(getattr(eid, "ServerIndex", 0) or 0)
    return ExpandedNodeId(node_id=inner, namespace_uri=uri or None, server_index=server)


def from_ua_qualified_name(qn: Any) -> QualifiedName:
    return QualifiedName(name=str(qn.Name), namespace_index=int(qn.NamespaceIndex))


def from_ua_localized_text(lt: Any) -> LocalizedText:
    return LocalizedText(text=lt.Text, locale=lt.Locale)


def from_ua_status_code(sc: Any) -> StatusCode:
    # asyncua surfaces both the numeric value and a name (e.g. "Good", "BadNoData").
    code = int(getattr(sc, "value", sc))
    raw_name = str(getattr(sc, "name", "Good") or "Good")
    # Normalize "BadNoData" -> "Bad_NoData" so the plan's symbol map matches.
    symbol = raw_name
    if symbol.startswith("Bad") and len(symbol) > 3 and symbol[3].isupper():
        symbol = "Bad_" + symbol[3:]
    elif symbol.startswith("Uncertain") and len(symbol) > 9 and symbol[9].isupper():
        symbol = "Uncertain_" + symbol[9:]
    return StatusCode(code=code, symbol=symbol)


def quality_from_status(sc: Any) -> Quality:
    status = from_ua_status_code(sc)
    if status.symbol == "Good":
        return Quality.Good
    if status.symbol.startswith("Bad"):
        return Quality.Bad
    return Quality.GoodNoData


# Minimal VariantType -> VariantType map. asyncua's IntEnum values match Part 6.
def _variant_type(raw: int) -> VariantType:
    try:
        return VariantType(int(raw))
    except ValueError:
        return VariantType.Null


def from_ua_variant(variant: Any) -> Variant:
    """Convert an asyncua.ua.Variant to our Variant.

    Structured sub-types (NodeId, QualifiedName, LocalizedText, StatusCode,
    ExtensionObject) are recursively converted; primitives pass through.
    """
    vt = _variant_type(int(variant.VariantType))
    body = _convert_value(vt, variant.Value, value_rank=_rank(variant))
    dims = tuple(variant.Dimensions) if getattr(variant, "Dimensions", None) else None
    return Variant(variant_type=vt, body=body, dimensions=dims)


def _rank(variant: Any) -> int:
    # ValueRank is not always present on a Variant; try both common attrs.
    rank = getattr(variant, "ValueRank", None)
    if rank is not None:
        return int(rank)
    return -1 if not isinstance(variant.Value, list | tuple) else 1


def _convert_value(vt: VariantType, value: Any, *, value_rank: int) -> Any:
    if value is None:
        return None
    if value_rank >= 1 and isinstance(value, list | tuple):
        return [_convert_scalar(vt, item) for item in value]
    return _convert_scalar(vt, value)


def _convert_scalar(vt: VariantType, value: Any) -> Any:
    if vt is VariantType.NodeId:
        return from_ua_node_id(value)
    if vt is VariantType.ExpandedNodeId:
        return from_ua_expanded_node_id(value)
    if vt is VariantType.QualifiedName:
        return from_ua_qualified_name(value)
    if vt is VariantType.LocalizedText:
        return from_ua_localized_text(value)
    if vt is VariantType.StatusCode:
        return from_ua_status_code(value)
    if vt is VariantType.ExtensionObject:
        return from_ua_extension_object(value)
    # Primitives + datetime + bytes pass through untouched.
    return value


def from_ua_extension_object(value: Any) -> ExtensionObject | dict[str, Any] | Any:
    """Best-effort ExtensionObject conversion.

    Three paths:
      1. asyncua generated a Python dataclass for this type (`ua_types` + `data_type`
         attrs present) — reconstruct a `StructureDefinition` and field dict.
      2. asyncua delivered a raw `ua.ExtensionObject` with `Body=<bytes>` because
         its codegen failed for the DataType ( value-side). We return a
         JSON-safe placeholder dict so `encode_variant`'s passthrough path emits
         a Part-6-ish shape without tripping Pydantic on raw non-UTF-8 bytes.
      3. Anything else passes through unchanged.
    """
    fields_meta = getattr(value, "ua_types", None)  # asyncua convention
    data_type = getattr(value, "data_type", None)
    if fields_meta is not None and data_type is not None:
        struct_fields: list[StructureField] = []
        field_values: dict[str, Any] = {}
        for field_name, ua_type_id in fields_meta:
            struct_fields.append(StructureField(name=field_name, data_type_node_id=str(ua_type_id)))
            field_values[field_name] = getattr(value, field_name, None)
        return ExtensionObject(
            definition=StructureDefinition(
                type_id=from_ua_node_id(data_type), fields=tuple(struct_fields)
            ),
            fields=field_values,
        )

    # fallback for the "raw ExtensionObject" asyncua surfaces when it can't
    # decode the structure. Preserve the TypeId + base64 bytes so consumers see
    # *something* useful; mark the body so downstream tooling can recognize it.
    type_id = getattr(value, "TypeId", None)
    body = getattr(value, "Body", None)
    if type_id is not None and isinstance(body, (bytes, bytearray)):
        return {
            "TypeId": encode_node_id(from_ua_node_id(type_id)),
            "Body": {
                "__unresolvedBinaryBody__": base64.b64encode(bytes(body)).decode("ascii"),
                "__note__": "DataType definition unavailable; raw binary body preserved",
            },
        }

    return value


def from_ua_data_value(dv: Any) -> DataValue:
    return DataValue(
        value=from_ua_variant(dv.Value),
        status=from_ua_status_code(dv.StatusCode),
        source_timestamp=dv.SourceTimestamp,
        source_picoseconds=int(getattr(dv, "SourcePicoseconds", 0) or 0),
        server_timestamp=dv.ServerTimestamp,
        server_picoseconds=int(getattr(dv, "ServerPicoseconds", 0) or 0),
    )


__all__ = [
    "from_ua_data_value",
    "from_ua_expanded_node_id",
    "from_ua_extension_object",
    "from_ua_localized_text",
    "from_ua_node_id",
    "from_ua_qualified_name",
    "from_ua_status_code",
    "from_ua_variant",
    "quality_from_status",
]
