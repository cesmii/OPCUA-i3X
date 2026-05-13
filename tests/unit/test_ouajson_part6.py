"""OPC UA Part 6 reversible JSON encoder tests."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from uuid import UUID

import pytest

from i3xua.ouajson import (
    DataValue,
    ExpandedNodeId,
    LocalizedText,
    NodeId,
    NodeIdType,
    QualifiedName,
    StatusCode,
    Variant,
    VariantType,
    encode_byte_string,
    encode_data_value,
    encode_datetime,
    encode_expanded_node_id,
    encode_guid,
    encode_localized_text,
    encode_node_id,
    encode_qualified_name,
    encode_status_code,
    encode_variant,
)

# primitives --------------------------------------------------------


def test_datetime_naive_is_treated_as_utc() -> None:
    dt = datetime(2026, 4, 14, 0, 15, 30)
    assert encode_datetime(dt) == "2026-04-14T00:15:30Z"


def test_datetime_with_microseconds_preserves_six_digits() -> None:
    dt = datetime(2026, 4, 14, 0, 15, 30, 123456, tzinfo=UTC)
    assert encode_datetime(dt) == "2026-04-14T00:15:30.123456Z"


def test_guid_is_uppercase_hyphenated() -> None:
    g = UUID("72962B91-FA75-4AE6-8D28-B404DC7DAF63")
    assert encode_guid(g) == "72962B91-FA75-4AE6-8D28-B404DC7DAF63"


def test_byte_string_is_base64() -> None:
    assert encode_byte_string(b"hello") == "aGVsbG8="


def test_int64_variant_emits_string_body() -> None:
    v = Variant(VariantType.Int64, 9223372036854775807)
    assert encode_variant(v) == {"Type": 8, "Body": "9223372036854775807"}


def test_uint64_variant_emits_string_body() -> None:
    v = Variant(VariantType.UInt64, 18446744073709551615)
    assert encode_variant(v) == {"Type": 9, "Body": "18446744073709551615"}


def test_float_special_values_are_strings() -> None:
    for raw, expected in [(math.inf, "Infinity"), (-math.inf, "-Infinity"), (math.nan, "NaN")]:
        v = Variant(VariantType.Double, raw)
        assert encode_variant(v) == {"Type": 11, "Body": expected}


def test_bool_variant_roundtrips_to_json_bool() -> None:
    assert encode_variant(Variant(VariantType.Boolean, True)) == {"Type": 1, "Body": True}
    assert encode_variant(Variant(VariantType.Boolean, False)) == {"Type": 1, "Body": False}


def test_int32_variant_is_plain_integer() -> None:
    assert encode_variant(Variant(VariantType.Int32, -42)) == {"Type": 6, "Body": -42}


def test_string_variant_supports_non_ascii() -> None:
    assert encode_variant(Variant(VariantType.String, "\u00e9ch\u00e9")) == {
        "Type": 12,
        "Body": "\u00e9ch\u00e9",
    }


# NodeId ----------------------------------------------------------


def test_node_id_numeric_default_namespace_omits_namespace_key() -> None:
    assert encode_node_id(NodeId(85)) == {"IdType": 0, "Id": 85}


def test_node_id_string_with_namespace() -> None:
    nid = NodeId(id="Boilers/Boiler1/Temperature", namespace=2)
    assert encode_node_id(nid) == {
        "IdType": 1,
        "Id": "Boilers/Boiler1/Temperature",
        "Namespace": 2,
    }


def test_node_id_guid_identifier() -> None:
    guid = UUID("72962B91-FA75-4AE6-8D28-B404DC7DAF63")
    assert encode_node_id(NodeId(id=guid, namespace=3)) == {
        "IdType": 2,
        "Id": "72962B91-FA75-4AE6-8D28-B404DC7DAF63",
        "Namespace": 3,
    }


def test_node_id_opaque_bytestring() -> None:
    assert encode_node_id(NodeId(id=b"\x01\x02\x03", namespace=4)) == {
        "IdType": 3,
        "Id": "AQID",
        "Namespace": 4,
    }


def test_node_id_explicit_id_type_override() -> None:
    # Caller can force Numeric even if id is a string, though we guard against
    # unusable combinations at encode time.
    nid = NodeId(id=1234, id_type=NodeIdType.Numeric)
    assert encode_node_id(nid) == {"IdType": 0, "Id": 1234}


# ExpandedNodeId / QualifiedName / LocalizedText ------------------


def test_expanded_node_id_with_namespace_uri_replaces_index() -> None:
    eid = ExpandedNodeId(node_id=NodeId(id="X", namespace=5), namespace_uri="urn:test:ns")
    assert encode_expanded_node_id(eid) == {
        "IdType": 1,
        "Id": "X",
        "Namespace": "urn:test:ns",
    }


def test_expanded_node_id_with_server_uri() -> None:
    eid = ExpandedNodeId(node_id=NodeId(42), server_index=7)
    assert encode_expanded_node_id(eid) == {"IdType": 0, "Id": 42, "ServerUri": 7}


def test_qualified_name_default_namespace_omits_uri() -> None:
    assert encode_qualified_name(QualifiedName(name="Temperature")) == {"Name": "Temperature"}


def test_qualified_name_with_index() -> None:
    assert encode_qualified_name(QualifiedName(name="T", namespace_index=2)) == {
        "Name": "T",
        "Uri": 2,
    }


def test_localized_text_emits_only_provided_fields() -> None:
    assert encode_localized_text(LocalizedText(text="Hello", locale="en")) == {
        "Locale": "en",
        "Text": "Hello",
    }


def test_localized_text_empty_is_empty_object() -> None:
    assert encode_localized_text(LocalizedText()) == {}


def test_localized_text_text_only() -> None:
    assert encode_localized_text(LocalizedText(text="Hi")) == {"Text": "Hi"}


# StatusCode (i3X three-valued vocabulary) ----------------


def test_status_code_good_is_string_good() -> None:
    assert encode_status_code(StatusCode()) == "Good"


def test_status_code_bad_prefix_collapses_to_bad() -> None:
    assert encode_status_code(StatusCode(code=0x80340000, symbol="Bad_NoData")) == "Bad"
    assert encode_status_code(StatusCode(code=0x80350000, symbol="Bad_AccessDenied")) == "Bad"


def test_status_code_uncertain_collapses_to_goodnodata() -> None:
    assert encode_status_code(StatusCode(code=0x40000000, symbol="Uncertain")) == "GoodNoData"
    assert encode_status_code(StatusCode(symbol="Uncertain_InitialValue")) == "GoodNoData"


def test_status_code_goodnodata_preserved() -> None:
    assert encode_status_code(StatusCode(symbol="GoodNoData")) == "GoodNoData"


# Variant arrays / matrices ---------------------------------------


def test_variant_scalar_has_no_dimensions_key() -> None:
    assert encode_variant(Variant(VariantType.String, "hi")) == {"Type": 12, "Body": "hi"}


def test_variant_flat_array() -> None:
    assert encode_variant(Variant(VariantType.Int32, [1, 2, 3])) == {
        "Type": 6,
        "Body": [1, 2, 3],
    }


def test_variant_matrix_flattens_row_major_with_dimensions() -> None:
    matrix = [[1, 2, 3], [4, 5, 6]]
    v = Variant(VariantType.Int32, matrix, dimensions=(2, 3))
    assert encode_variant(v) == {
        "Type": 6,
        "Body": [1, 2, 3, 4, 5, 6],
        "Dimensions": [2, 3],
    }


def test_variant_array_with_explicit_one_dimensional_shape() -> None:
    v = Variant(VariantType.Double, [1.5, 2.5], dimensions=(2,))
    assert encode_variant(v) == {
        "Type": 11,
        "Body": [1.5, 2.5],
        "Dimensions": [2],
    }


# DataValue -------------------------------------------------------


def test_data_value_value_only_omits_defaults() -> None:
    dv = DataValue(value=Variant(VariantType.Int32, 7))
    assert encode_data_value(dv) == {"Value": {"Type": 6, "Body": 7}}


def test_data_value_emits_timestamps_and_bad_status() -> None:
    dv = DataValue(
        value=Variant(VariantType.Double, 3.14),
        status=StatusCode(code=0x40000000, symbol="Uncertain"),
        source_timestamp=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        source_picoseconds=123,
        server_timestamp=datetime(2026, 1, 2, 3, 4, 6, tzinfo=UTC),
        server_picoseconds=0,
    )
    assert encode_data_value(dv) == {
        "Value": {"Type": 11, "Body": 3.14},
        "Status": "GoodNoData",  # Uncertain collapses to GoodNoData per
        "SourceTimestamp": "2026-01-02T03:04:05Z",
        "SourcePicoseconds": 123,
        "ServerTimestamp": "2026-01-02T03:04:06Z",
    }


def test_data_value_with_variant_of_localized_text() -> None:
    dv = DataValue(value=Variant(VariantType.LocalizedText, LocalizedText(text="Hi", locale="en")))
    assert encode_data_value(dv) == {"Value": {"Type": 21, "Body": {"Locale": "en", "Text": "Hi"}}}


# Guard against a regression: unsupported types must fail loudly -------------


def test_unsupported_variant_type_raises() -> None:
    with pytest.raises(ValueError, match="unsupported VariantType"):
        encode_variant(Variant(VariantType.DiagnosticInfo, "unused"))
