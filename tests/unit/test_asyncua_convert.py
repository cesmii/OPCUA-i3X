"""asyncua -> ouajson type conversion.

Duck-types faux `ua.*` objects so the tests don't need the real library.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from i3xua.adapters.asyncua.convert import (
    from_ua_data_value,
    from_ua_expanded_node_id,
    from_ua_extension_object,
    from_ua_localized_text,
    from_ua_node_id,
    from_ua_qualified_name,
    from_ua_status_code,
    from_ua_variant,
    quality_from_status,
)
from i3xua.core.neutral import Quality
from i3xua.ouajson.types import ExtensionObject, NodeIdType, VariantType


@dataclass
class _UANodeId:
    Identifier: Any
    NamespaceIndex: int
    NodeIdType: int  # 0..3


@dataclass
class _UAQName:
    Name: str
    NamespaceIndex: int


@dataclass
class _UALocalized:
    Text: str
    Locale: str | None


@dataclass
class _UAStatus:
    value: int
    name: str


@dataclass
class _UAVariant:
    Value: Any
    VariantType: int
    Dimensions: list[int] | None = None


@dataclass
class _UADataValue:
    Value: _UAVariant
    StatusCode: _UAStatus
    SourceTimestamp: datetime | None
    ServerTimestamp: datetime | None
    SourcePicoseconds: int = 0
    ServerPicoseconds: int = 0


# ------------------------------------------------------------------ tests


def test_node_id_numeric() -> None:
    nid = from_ua_node_id(_UANodeId(Identifier=85, NamespaceIndex=0, NodeIdType=0))
    assert nid.namespace == 0
    assert nid.id == 85
    assert nid.id_type is NodeIdType.Numeric


def test_node_id_string_with_namespace() -> None:
    # asyncua's NodeIdType=3 means String (its enum is 6-valued, not Part-6's 4).
    nid = from_ua_node_id(_UANodeId(Identifier="Boiler/Temp", NamespaceIndex=2, NodeIdType=3))
    assert nid.id == "Boiler/Temp"
    assert nid.namespace == 2
    assert nid.id_type is NodeIdType.String


def test_node_id_asyncua_fourbyte_is_numeric_not_string() -> None:
    # Regression guard: asyncua `FourByte=1` MUST map to our Part-6 `Numeric`,
    # not `String`. Prior mismatched mapping caused 500s on every numeric
    # NodeId arriving via asyncua (e.g. ExtensionObject.TypeId).
    nid = from_ua_node_id(_UANodeId(Identifier=3515, NamespaceIndex=3, NodeIdType=1))
    assert nid.id == 3515
    assert nid.id_type is NodeIdType.Numeric


def test_qualified_name_and_localized_text() -> None:
    assert from_ua_qualified_name(_UAQName(Name="Temp", NamespaceIndex=2)).namespace_index == 2
    lt = from_ua_localized_text(_UALocalized(Text="Hi", Locale="en"))
    assert lt.text == "Hi"
    assert lt.locale == "en"


def test_status_code_symbol_is_underscored_for_bad() -> None:
    sc = from_ua_status_code(_UAStatus(value=0x80340000, name="BadNoData"))
    assert sc.symbol == "Bad_NoData"
    assert sc.code == 0x80340000


def test_status_code_good_stays_good() -> None:
    sc = from_ua_status_code(_UAStatus(value=0, name="Good"))
    assert sc.symbol == "Good"


def test_status_code_uncertain_is_normalized() -> None:
    sc = from_ua_status_code(_UAStatus(value=0x40000000, name="UncertainInitialValue"))
    assert sc.symbol == "Uncertain_InitialValue"


def test_quality_from_status_maps_to_quality_enum() -> None:
    assert quality_from_status(_UAStatus(value=0, name="Good")) is Quality.Good
    assert quality_from_status(_UAStatus(value=0x80340000, name="BadNoData")) is Quality.Bad
    assert quality_from_status(_UAStatus(value=0x40000000, name="Uncertain")) is Quality.GoodNoData


def test_variant_scalar_primitive_passes_through() -> None:
    v = from_ua_variant(_UAVariant(Value=42, VariantType=int(VariantType.Int32)))
    assert v.variant_type is VariantType.Int32
    assert v.body == 42
    assert v.dimensions is None


def test_variant_nested_node_id_is_converted() -> None:
    ua_nid = _UANodeId(Identifier="X", NamespaceIndex=1, NodeIdType=1)
    v = from_ua_variant(_UAVariant(Value=ua_nid, VariantType=int(VariantType.NodeId)))
    assert v.variant_type is VariantType.NodeId
    assert v.body.id == "X"
    assert v.body.namespace == 1


def test_variant_array_preserves_list() -> None:
    v = from_ua_variant(_UAVariant(Value=[1, 2, 3], VariantType=int(VariantType.Int32)))
    assert v.body == [1, 2, 3]


def test_data_value_wraps_everything() -> None:
    dv = from_ua_data_value(
        _UADataValue(
            Value=_UAVariant(Value=3.14, VariantType=int(VariantType.Double)),
            StatusCode=_UAStatus(value=0, name="Good"),
            SourceTimestamp=datetime(2026, 4, 14, 1, 0, 0, tzinfo=UTC),
            ServerTimestamp=None,
        )
    )
    assert dv.value.variant_type is VariantType.Double
    assert dv.value.body == 3.14
    assert dv.status.symbol == "Good"
    assert dv.server_timestamp is None


# ------------------------------------------------------------------ ExtensionObject


def test_extension_object_with_asyncua_metadata_converts_to_our_structure() -> None:
    class _FakeAsyncuaStruct:
        data_type = _UANodeId(Identifier="BoilerType", NamespaceIndex=2, NodeIdType=1)
        ua_types = (("Temp", "ns=0;i=11"), ("Label", "ns=0;i=12"))
        Temp = 88.1
        Label = "Plant-A"

    eo = from_ua_extension_object(_FakeAsyncuaStruct())
    assert isinstance(eo, ExtensionObject)
    assert eo.definition.type_id.id == "BoilerType"
    assert [f.name for f in eo.definition.fields] == ["Temp", "Label"]
    assert eo.fields == {"Temp": 88.1, "Label": "Plant-A"}


def test_extension_object_without_metadata_passes_through() -> None:
    # Plain dicts / unrecognized shapes come back unchanged so the encoder
    # can still produce JSON (best-effort).
    passthrough = {"Temp": 1.0}
    assert from_ua_extension_object(passthrough) is passthrough


def test_extension_object_with_raw_bytes_body_is_base64_placeholder() -> None:
    """value-side fallback: when asyncua can't decode the structure it
    surfaces a `ua.ExtensionObject(TypeId=..., Body=<raw bytes>)`. Our converter
    MUST NOT pass the raw bytes through — they aren't JSON-serializable. Emit a
    placeholder dict with the TypeId encoded and the bytes base64-stashed."""

    class _RawExtObj:
        def __init__(self) -> None:
            self.TypeId = _UANodeId(Identifier=3515, NamespaceIndex=3, NodeIdType=0)
            self.Body = b"\x00\xff\x80garbage-not-utf8"

    out = from_ua_extension_object(_RawExtObj())
    assert isinstance(out, dict)
    assert out["TypeId"] == {"IdType": 0, "Id": 3515, "Namespace": 3}
    assert "__unresolvedBinaryBody__" in out["Body"]
    # The base64 encoding must round-trip the original bytes verbatim.
    import base64

    assert (
        base64.b64decode(out["Body"]["__unresolvedBinaryBody__"]) == b"\x00\xff\x80garbage-not-utf8"
    )


def test_expanded_node_id_carries_namespace_uri() -> None:
    @dataclass
    class _UAExpanded:
        Identifier: Any
        NamespaceIndex: int
        NodeIdType: int
        NamespaceUri: str | None = None
        ServerIndex: int = 0

    eid = from_ua_expanded_node_id(
        _UAExpanded(
            Identifier=5, NamespaceIndex=2, NodeIdType=0, NamespaceUri="urn:x", ServerIndex=3
        )
    )
    assert eid.namespace_uri == "urn:x"
    assert eid.server_index == 3
    assert eid.node_id.id == 5
