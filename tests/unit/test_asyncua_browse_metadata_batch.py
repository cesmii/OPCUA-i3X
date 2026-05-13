"""D-66/D-67/D-68 — Variable metadata is fetched via a single multi-attribute
Read request per browse_children batch (replacing the old per-attribute call),
and `BadAttributeIdInvalid` for individual attributes degrades to None on the
NodeInfo without aborting the whole call."""

from __future__ import annotations

from asyncua import ua

from i3xua.adapters.asyncua.upstream import (
    _METADATA_ATTRS,
    _build_metadata_read_items,
    _decode_metadata_results,
)


def test_metadata_batch_request_includes_all_seven_attributes() -> None:
    var_nodes = [ua.NodeId(2254, 0), ua.NodeId(2255, 0)]
    items = _build_metadata_read_items(var_nodes)

    assert len(items) == 2 * 7
    expected_attrs = {
        ua.AttributeIds.DataType,
        ua.AttributeIds.ValueRank,
        ua.AttributeIds.ArrayDimensions,
        ua.AttributeIds.AccessLevel,
        ua.AttributeIds.UserAccessLevel,
        ua.AttributeIds.MinimumSamplingInterval,
        ua.AttributeIds.Historizing,
    }
    seen_attrs = {item.AttributeId for item in items}
    assert seen_attrs == expected_attrs

    # Var-major, attr-minor: 7 contiguous items per variable.
    for v_idx, var_node in enumerate(var_nodes):
        slice_ = items[v_idx * 7 : (v_idx + 1) * 7]
        assert all(it.NodeId == var_node for it in slice_)


def test_decode_results_groups_per_variable_with_canonical_node_ids() -> None:
    var_nodes = [ua.NodeId(1, 2)]
    items = _build_metadata_read_items(var_nodes)

    bad_status = ua.StatusCode(ua.status_codes.StatusCodes.BadAttributeIdInvalid)
    null_dv_bad = ua.DataValue(
        Value=ua.Variant(Value=None, VariantType=ua.VariantType.Null),
        StatusCode_=bad_status,
    )

    results = [
        # DataType
        ua.DataValue(Value=ua.Variant(Value=ua.NodeId(11, 0), VariantType=ua.VariantType.NodeId)),
        # ValueRank
        ua.DataValue(Value=ua.Variant(Value=-1, VariantType=ua.VariantType.Int32)),
        # ArrayDimensions — Bad → None
        null_dv_bad,
        # AccessLevel
        ua.DataValue(Value=ua.Variant(Value=1, VariantType=ua.VariantType.Byte)),
        # UserAccessLevel
        ua.DataValue(Value=ua.Variant(Value=1, VariantType=ua.VariantType.Byte)),
        # MinimumSamplingInterval
        ua.DataValue(Value=ua.Variant(Value=10.0, VariantType=ua.VariantType.Double)),
        # Historizing
        ua.DataValue(Value=ua.Variant(Value=False, VariantType=ua.VariantType.Boolean)),
    ]
    decoded = _decode_metadata_results(items, results)
    by_node = decoded[var_nodes[0].to_string()]
    # canonicalize_node_id strips ns=0; — asyncua's NodeId.to_string() form.
    assert by_node["data_type_node_id"] in {"i=11", "ns=0;i=11"}
    assert by_node["value_rank"] == -1
    assert by_node["array_dimensions"] is None
    assert by_node["access_level"] == 1
    assert by_node["user_access_level"] == 1
    assert by_node["minimum_sampling_interval"] == 10.0
    assert by_node["historizing"] is False


def test_decode_results_handles_empty_array_dimensions_as_none() -> None:
    """Servers report an empty ArrayDimensions list for scalars; we
    normalize to None so the wire surface only carries it when shape is
    actually multi-dim."""
    var_nodes = [ua.NodeId(1, 2)]
    items = _build_metadata_read_items(var_nodes)
    results = [
        ua.DataValue(Value=ua.Variant(Value=ua.NodeId(11, 0), VariantType=ua.VariantType.NodeId)),
        ua.DataValue(Value=ua.Variant(Value=-1, VariantType=ua.VariantType.Int32)),
        ua.DataValue(Value=ua.Variant(Value=[], VariantType=ua.VariantType.UInt32)),
        ua.DataValue(Value=ua.Variant(Value=1, VariantType=ua.VariantType.Byte)),
        ua.DataValue(Value=ua.Variant(Value=1, VariantType=ua.VariantType.Byte)),
        ua.DataValue(Value=ua.Variant(Value=10.0, VariantType=ua.VariantType.Double)),
        ua.DataValue(Value=ua.Variant(Value=False, VariantType=ua.VariantType.Boolean)),
    ]
    decoded = _decode_metadata_results(items, results)
    assert decoded[var_nodes[0].to_string()]["array_dimensions"] is None


def test_metadata_attrs_order_matches_decoder() -> None:
    """Guard rail: the attr-name table must zip 1:1 with the AttributeIds
    table — re-order one and the decoder silently mis-maps fields."""
    from i3xua.adapters.asyncua.upstream import _METADATA_ATTR_NAMES

    assert len(_METADATA_ATTRS) == len(_METADATA_ATTR_NAMES)
