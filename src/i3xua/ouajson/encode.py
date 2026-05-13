"""OPC UA Part 6 §5.4 reversible JSON encoders.

Deviations from strict Part 6:
- StatusCode is emitted as `{"Code", "Symbol"}` in all positions (strict reversible uses
  a bare integer). The object form matches what i3X-Explorer expects.
"""

from __future__ import annotations

import base64
import math
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from i3xua.ouajson.types import (
    DataValue,
    ExpandedNodeId,
    ExtensionObject,
    LocalizedText,
    NodeId,
    NodeIdType,
    QualifiedName,
    StatusCode,
    StructureField,
    Variant,
    VariantType,
)

# Mapping from builtin DataType NodeId -> VariantType used when encoding
# ExtensionObject fields. Kept local to avoid a cross-module cycle with
# `adapters.asyncua.typemap`.
_BUILTIN_FIELD_VARIANT: dict[str, VariantType] = {
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
}

INT32_MIN = -(2**31)
INT32_MAX = 2**31 - 1
UINT32_MAX = 2**32 - 1


def encode(value: Any) -> Any:
    """Dispatch on dataclass type; fall through to JSON-native for primitives."""
    if isinstance(value, DataValue):
        return encode_data_value(value)
    if isinstance(value, Variant):
        return encode_variant(value)
    if isinstance(value, ExtensionObject):
        return encode_extension_object(value)
    if isinstance(value, NodeId):
        return encode_node_id(value)
    if isinstance(value, ExpandedNodeId):
        return encode_expanded_node_id(value)
    if isinstance(value, QualifiedName):
        return encode_qualified_name(value)
    if isinstance(value, LocalizedText):
        return encode_localized_text(value)
    if isinstance(value, StatusCode):
        return encode_status_code(value)
    if isinstance(value, datetime):
        return encode_datetime(value)
    if isinstance(value, UUID):
        return encode_guid(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return encode_byte_string(bytes(value))
    return value


# ------------------------------------------------------------------ primitives


def encode_datetime(dt: datetime) -> str:
    """ISO 8601 with UTC 'Z' suffix; naive datetimes are assumed UTC."""
    dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    # Match Part 6 example format; keep microseconds when present.
    if dt.microsecond:
        stamp = dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{dt.microsecond:06d}"
    else:
        stamp = dt.strftime("%Y-%m-%dT%H:%M:%S")
    return f"{stamp}Z"


def encode_guid(guid: UUID) -> str:
    # Part 6 §5.4.2.7 requires uppercase hyphenated form per the examples.
    return str(guid).upper()


def encode_byte_string(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def encode_int64(value: int) -> str:
    # JSON numbers can't safely represent full 64-bit range, per Part 6 §5.4.2.
    return str(value)


def encode_float(value: float) -> float | str:
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    return value


# ------------------------------------------------------------------ structured types


def encode_node_id(node_id: NodeId) -> dict[str, Any]:
    id_type = node_id.resolved_type()
    body: Any
    if id_type is NodeIdType.Numeric:
        assert isinstance(node_id.id, int)
        body = node_id.id
    elif id_type is NodeIdType.String:
        assert isinstance(node_id.id, str)
        body = node_id.id
    elif id_type is NodeIdType.Guid:
        assert isinstance(node_id.id, UUID)
        body = encode_guid(node_id.id)
    elif id_type is NodeIdType.Opaque:
        assert isinstance(node_id.id, (bytes, bytearray))
        body = encode_byte_string(bytes(node_id.id))
    else:  # pragma: no cover - exhaustiveness
        raise ValueError(f"unknown NodeIdType: {id_type}")

    out: dict[str, Any] = {"IdType": int(id_type), "Id": body}
    if node_id.namespace:
        out["Namespace"] = node_id.namespace
    return out


def encode_expanded_node_id(eid: ExpandedNodeId) -> dict[str, Any]:
    out = encode_node_id(eid.node_id)
    if eid.namespace_uri:
        # Per Part 6, a NamespaceUri overrides the Namespace index.
        out.pop("Namespace", None)
        out["Namespace"] = eid.namespace_uri
    if eid.server_index:
        out["ServerUri"] = eid.server_index
    return out


def encode_qualified_name(qn: QualifiedName) -> dict[str, Any]:
    out: dict[str, Any] = {"Name": qn.name}
    if qn.namespace_index:
        out["Uri"] = qn.namespace_index
    return out


def encode_localized_text(lt: LocalizedText) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if lt.locale:
        out["Locale"] = lt.locale
    if lt.text is not None:
        out["Text"] = lt.text
    return out


def encode_status_code(sc: StatusCode) -> str:
    """Collapse OPC UA StatusCode onto i3X's three-valued quality vocabulary.

    The i3X wire surface never carries the raw `Code` or the full `{Good, Bad_*,
    Uncertain_*}` symbol — only `"Good" | "GoodNoData" | "Bad"`. This keeps
    every StatusCode occurrence (including inside Variant bodies whose
    VariantType is StatusCode) consistent with the VQT.quality contract.
    """
    symbol = sc.symbol
    if symbol == "Good":
        return "Good"
    if symbol.startswith("Bad"):
        return "Bad"
    # Uncertain*, GoodNoData, anything else falls into the "stale / no data" bucket.
    return "GoodNoData"


# ------------------------------------------------------------------ Variant / DataValue


def _encode_scalar(variant_type: VariantType, value: Any) -> Any:
    if value is None:
        return None
    if variant_type is VariantType.Boolean:
        return bool(value)
    if variant_type in (
        VariantType.SByte,
        VariantType.Byte,
        VariantType.Int16,
        VariantType.UInt16,
        VariantType.Int32,
        VariantType.UInt32,
    ):
        return int(value)
    if variant_type in (VariantType.Int64, VariantType.UInt64):
        return encode_int64(int(value))
    if variant_type in (VariantType.Float, VariantType.Double):
        return encode_float(float(value))
    if variant_type is VariantType.String:
        return str(value)
    if variant_type is VariantType.DateTime:
        return encode_datetime(value)
    if variant_type is VariantType.Guid:
        return encode_guid(value if isinstance(value, UUID) else UUID(str(value)))
    if variant_type is VariantType.ByteString:
        return encode_byte_string(bytes(value))
    if variant_type is VariantType.XmlElement:
        return str(value)
    if variant_type is VariantType.NodeId:
        return encode_node_id(value)
    if variant_type is VariantType.ExpandedNodeId:
        return encode_expanded_node_id(value)
    if variant_type is VariantType.StatusCode:
        return encode_status_code(value)
    if variant_type is VariantType.QualifiedName:
        return encode_qualified_name(value)
    if variant_type is VariantType.LocalizedText:
        return encode_localized_text(value)
    if variant_type is VariantType.Variant:
        return encode_variant(value)
    if variant_type is VariantType.DataValue:
        return encode_data_value(value)
    if variant_type is VariantType.ExtensionObject:
        if isinstance(value, ExtensionObject):
            return encode_extension_object(value)
        # Accept pre-encoded dicts for callers that already produced a Part-6 body.
        return value
    if variant_type is VariantType.Null:
        return None
    raise ValueError(f"unsupported VariantType for scalar: {variant_type!r}")


def _flatten_matrix(value: Any) -> list[Any]:
    """Row-major flatten of nested lists."""
    out: list[Any] = []
    if isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, (list, tuple)):
                out.extend(_flatten_matrix(item))
            else:
                out.append(item)
    else:
        out.append(value)
    return out


def _encode_body(variant_type: VariantType, body: Any, is_array: bool) -> Any:
    if not is_array:
        return _encode_scalar(variant_type, body)
    if not isinstance(body, Iterable):
        raise TypeError(f"array variant body must be iterable, got {type(body).__name__}")
    return [_encode_scalar(variant_type, item) for item in body]


def encode_variant(variant: Variant) -> dict[str, Any]:
    dims = variant.dimensions
    if dims is not None and len(dims) > 1:
        flat = _flatten_matrix(variant.body)
        body = [_encode_scalar(variant.variant_type, item) for item in flat]
        return {
            "Type": int(variant.variant_type),
            "Body": body,
            "Dimensions": list(dims),
        }
    is_array = isinstance(variant.body, (list, tuple)) or (dims is not None and len(dims) == 1)
    body = _encode_body(variant.variant_type, variant.body, is_array)
    out: dict[str, Any] = {"Type": int(variant.variant_type), "Body": body}
    if dims is not None:
        out["Dimensions"] = list(dims)
    return out


def _encode_field(field: StructureField, value: Any) -> Any:
    variant = _BUILTIN_FIELD_VARIANT.get(field.data_type_node_id)
    if value is None and field.is_optional:
        return None
    if isinstance(value, ExtensionObject):
        return encode_extension_object(value)
    if variant is None:
        # Unknown / custom DataType: best-effort passthrough via top-level encode().
        return encode(value)
    if field.value_rank >= 1:
        if value is None:
            return None
        return [_encode_scalar(variant, item) for item in value]
    return _encode_scalar(variant, value)


def encode_extension_object(eo: ExtensionObject) -> dict[str, Any]:
    """Emit `{TypeId, Body}` per Part 6 §5.4.2.12 (JSON encoding)."""
    body: dict[str, Any] = {}
    for fld in eo.definition.fields:
        if fld.is_optional and fld.name not in eo.fields:
            continue
        body[fld.name] = _encode_field(fld, eo.fields.get(fld.name))
    return {"TypeId": encode_node_id(eo.definition.type_id), "Body": body}


def encode_data_value(dv: DataValue) -> dict[str, Any]:
    out: dict[str, Any] = {"Value": encode_variant(dv.value)}
    # Omit defaults per Part 6 §5.4.2.9 to keep the payload small.
    if dv.status != StatusCode(0, "Good"):
        out["Status"] = encode_status_code(dv.status)
    if dv.source_timestamp is not None:
        out["SourceTimestamp"] = encode_datetime(dv.source_timestamp)
    if dv.source_picoseconds:
        out["SourcePicoseconds"] = dv.source_picoseconds
    if dv.server_timestamp is not None:
        out["ServerTimestamp"] = encode_datetime(dv.server_timestamp)
    if dv.server_picoseconds:
        out["ServerPicoseconds"] = dv.server_picoseconds
    return out
