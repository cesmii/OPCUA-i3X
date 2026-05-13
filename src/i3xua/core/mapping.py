"""Neutral adapter types -> i3X Pydantic wire models.

Pure functions; no I/O, no asyncua, no framework deps. This is the SINGLE
translation point between the hexagonal adapter boundary (`core.neutral`)
and the wire contract (`i3x.types`). Routes call these functions — they
must NOT re-derive the same shapes independently.

Coupling profile:
  - High efferent: depends on `core.neutral` + `i3x.types`.
  - Medium afferent: routes + tests depend on these functions.
  - NO coupling to adapters, asyncua, or FastAPI.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from i3xua.core.datatype_names import lookup_datatype_name
from i3xua.core.neutral import (
    ElementRef,
    NamespaceInfo,
    NodeClass,
    NodeDescriptor,
    Quality,
    TypeDescriptor,
    ValueSample,
)
from i3xua.i3x.types import (
    VQT,
    CurrentValueResult,
    HistoricalValueResult,
    Namespace,
    ObjectInstance,
    ObjectInstanceMetadata,
    ObjectType,
)


def _connection_ns_uri(uri: str, connection: str) -> str:
    """Collision-safe i3X namespace URI: ``<ua-uri>#connection=<name>``.

    Two servers can publish the same custom URI but mean different things —
    the connection suffix keeps types/instances from accidentally colliding
    in the registry across connections.
    """
    return f"{uri}#connection={connection}"


# ------------------------------------------------------------------ namespaces


def to_namespace(info: NamespaceInfo) -> Namespace:
    return Namespace(
        uri=info.uri,
        displayName=info.display_name,
    )


def to_namespaces(infos: Iterable[NamespaceInfo]) -> list[Namespace]:
    return [to_namespace(i) for i in infos]


# ------------------------------------------------------------------ object types


def to_object_type(t: TypeDescriptor) -> ObjectType:
    # elementId uses the type's `display_name` (BrowseName) — `<conn>!PropertyType`
    # rather than `<conn>!i=68`. Human-readable; the actual NodeId stays
    # on the descriptor side for any internal lookup that needs it.
    element_id = ElementRef(connection=t.connection, node_id=t.display_name).as_id()
    return ObjectType(
        elementId=element_id,
        displayName=t.display_name,
        namespaceUri=_connection_ns_uri(t.namespace_uri, t.connection),
        schema=t.json_schema,
        sourceTypeId=t.display_name,
        version=t.version,
    )


def to_object_types(descriptors: Iterable[TypeDescriptor]) -> list[ObjectType]:
    return [to_object_type(t) for t in descriptors]


# ------------------------------------------------------------------ object instances

_EXTENDED_ATTR_FALLBACK_SCHEMA: dict[str, Any] = {
    "type": "string",
    "description": "CSV field values until type is resolved",
}


def _access_level_to_string(access_level: int) -> str:
    """OPC UA Part 3 §5.6.2: bit 0 = CurrentRead, bit 1 = CurrentWrite.

    HistoricalRead/Write and the other higher bits are ignored — historizing
    is exposed separately, the rest are vanishingly rare on the wire and
    bound the artificial-type combinatorics.
    """
    has_read = bool(access_level & 0b01)
    has_write = bool(access_level & 0b10)
    if has_read and has_write:
        return "rw"
    if has_read:
        return "r"
    if has_write:
        return "w"
    return "none"


def _build_relationships(
    node: NodeDescriptor,
    parent_name: str | None,
    children: list[NodeDescriptor] | None,
) -> dict[str, Any] | None:
    """Build the spec `metadata.relationships` dict.

    Values use displayName (human-readable) — matching the spec examples
    which use names like "pump-101", "tank-201", not opaque index addresses.

    Spec example:
    ```json
    "relationships": {
      "HasParent": "pump-station",
      "HasChildren": ["pump-101", "tank-201", "sensor-001"]
    }
    ```
    """
    if children is None:
        return None

    rels: dict[str, Any] = {}

    # HasParent: always present if the node has a parent.
    if parent_name is not None:
        rels["HasParent"] = parent_name

    # HasChildren: all direct children regardless of relationship type.
    child_names = [c.display_name for c in children]
    if child_names:
        rels["HasChildren"] = child_names

    # HasComponent: children linked via HasComponent relationship.
    component_names = [c.display_name for c in children if c.parent_relationship == "HasComponent"]
    if component_names:
        rels["HasComponent"] = component_names

    # ComponentOf: if THIS node is a component of its parent.
    if parent_name is not None and node.parent_relationship == "HasComponent":
        rels["ComponentOf"] = parent_name

    return rels if rels else None


def to_object_instance(
    node: NodeDescriptor,
    *,
    types: dict[str, TypeDescriptor] | None = None,
    children: list[NodeDescriptor] | None = None,
    parent_display_name: str | None = None,
    artificial_types_enabled: bool = True,
) -> ObjectInstance:
    from i3xua.core.artificial_types import (
        artificial_type_name as _artificial_type_name,
    )
    from i3xua.core.artificial_types import (
        derive_shape as _derive_shape,
    )
    from i3xua.core.artificial_types import (
        should_replace as _should_replace,
    )

    # elementId / parentId use the wire-side NodeId-segment (display-path
    # for opaque numeric NodeIds; canonical form for already-readable
    # string NodeIds). Falls back to raw node_id for descriptors built
    # outside the browse pipeline (test fixtures).
    wire_nid = node.wire_node_id or node.node_id
    element_id = ElementRef(connection=node.connection, node_id=wire_nid).as_id()
    parent_id: str | None = None
    if node.parent_node_id is not None:
        parent_wire = node.parent_wire_node_id or node.parent_node_id
        parent_id = ElementRef(connection=node.connection, node_id=parent_wire).as_id()

    source_for_type = node.type_source_id if node.type_source_id is not None else "UnknownType"
    # When the server reported a generic TypeDefinition for a Variable AND
    # the parent has no meaningful type, point ``typeElementId`` at the
    # artificial type co-located with the source connection (no
    # cross-connection references).
    if (
        artificial_types_enabled
        and node.node_class == NodeClass.Variable
        and _should_replace(node.type_source_id)
        and node.data_type_node_id is not None
        and node.value_rank is not None
        and node.access_level is not None
    ):
        shape = _derive_shape(
            node.data_type_node_id,
            node.value_rank,
            node.access_level,
            dt_name_override=node.data_type_name,
        )
        type_element_id = ElementRef(
            connection=node.connection,
            node_id=_artificial_type_name(shape),
        ).as_id()
    else:
        # typeElementId NodeId-segment uses the type's `display_name` when
        # the types map can resolve it (e.g. `<conn>!PropertyType`), falling
        # back to the raw NodeId form when no map is provided. Mirrors the
        # ObjectType-side change in `to_object_type`.
        type_eid_segment = source_for_type
        if (
            types is not None
            and node.type_source_id is not None
            and (td := types.get(node.type_source_id)) is not None
        ):
            type_eid_segment = td.display_name
        type_element_id = ElementRef(connection=node.connection, node_id=type_eid_segment).as_id()

    # Build structured metadata per the reference ObjectInstanceMetadata model.
    ns_uri = _connection_ns_uri(node.namespace_uri, node.connection)
    extended_attrs: dict[str, Any] | None = None
    is_extended = False

    if types is not None and node.type_source_id is not None:
        descriptor = types.get(node.type_source_id)
        if descriptor is not None and descriptor.unresolved_fields:
            is_extended = True
            extended_attrs = {
                field_name: dict(_EXTENDED_ATTR_FALLBACK_SCHEMA)
                for field_name in descriptor.unresolved_fields
            }

    relationships = _build_relationships(node, parent_display_name, children)

    data_type_node_id: str | None = None
    data_type_name: str | None = None
    if node.node_class == NodeClass.Variable and node.data_type_node_id is not None:
        data_type_node_id = node.data_type_node_id
        # Prefer the browse-time-resolved name (covers Duration, UtcTime,
        # LocaleId, Decimal, …); fall back to the Part-6 builtin table; then
        # to a TypeRegistry lookup (rare — only when DataTypes happen to be
        # registered as ObjectType/VariableType, which they aren't normally).
        data_type_name = node.data_type_name or lookup_datatype_name(data_type_node_id)
        if data_type_name is None and types is not None:
            dt_descriptor = types.get(data_type_node_id)
            if dt_descriptor is not None:
                data_type_name = dt_descriptor.display_name

    # `sourceTypeId` is the human-readable type name (BrowseName/display_name
    # of the TypeDefinition). Resolves via the types map; falls back to the
    # raw NodeId when no map is provided (paths without registry context).
    if node.type_source_id is None:
        source_type_id = "UnknownType"
    elif types is not None and (type_descriptor := types.get(node.type_source_id)) is not None:
        source_type_id = type_descriptor.display_name
    else:
        source_type_id = node.type_source_id

    # Variable-only attribute extension for ``metadata.system``. Each field
    # is dropped when None so non-Variable nodes (and Variables whose
    # attribute Read returned BadAttributeIdInvalid for that field) carry
    # only the always-present ``nodeClass`` + ``sourceNodeId``.
    var_system: dict[str, Any] = {}
    if node.node_class == NodeClass.Variable:
        if node.access_level is not None:
            var_system["accessLevel"] = _access_level_to_string(node.access_level)
        if node.user_access_level is not None:
            var_system["userAccessLevel"] = _access_level_to_string(node.user_access_level)
        if node.value_rank is not None:
            var_system["valueRank"] = node.value_rank
        if node.array_dimensions is not None:
            var_system["arrayDimensions"] = list(node.array_dimensions)
        if node.historizing is not None:
            var_system["historizing"] = node.historizing
        if node.minimum_sampling_interval is not None:
            var_system["minimumSamplingInterval"] = node.minimum_sampling_interval

    system_dict: dict[str, Any] = {
        "nodeClass": node.node_class.value,
        "sourceNodeId": node.node_id,
        **var_system,
        **node.metadata,
    }

    metadata = ObjectInstanceMetadata(
        typeNamespaceUri=ns_uri,
        sourceTypeId=source_type_id,
        dataType=data_type_node_id,
        dataTypeName=data_type_name,
        description=node.description,
        extendedAttributes=extended_attrs,
        system=system_dict,
    )

    return ObjectInstance(
        elementId=element_id,
        displayName=node.display_name,
        typeElementId=type_element_id,
        parentId=parent_id,
        isComposition=node.is_composition,
        isExtended=is_extended,
        namespaceUri=ns_uri,
        relationships=relationships,
        metadata=metadata,
    )


def to_object_instances(
    nodes: Iterable[NodeDescriptor],
    *,
    types: dict[str, TypeDescriptor] | None = None,
    artificial_types_enabled: bool = True,
) -> list[ObjectInstance]:
    return [
        to_object_instance(n, types=types, artificial_types_enabled=artificial_types_enabled)
        for n in nodes
    ]


# ------------------------------------------------------------------ values


def _strip_variant(encoded: Any) -> Any:
    """Strip the Part-6 reversible Variant wrapper to expose the naked value.

    i3X convention (per the reference server models and CESMII SM Profile
    semantics): values on the wire are native JSON — numbers, strings,
    booleans, ISO-8601 date strings, or Part-6 complex-type objects (NodeId,
    LocalizedText) — but NEVER wrapped in the `{"Type": N, "Body": X}`
    reversible Variant envelope. That encoding detail belongs behind the
    adapter boundary, not on the consumer surface.

    Transforms:
      {"Type": 11, "Body": -0.97} → -0.97
      {"Type": 12, "Body": "hi"} → "hi"
      {"Type": 1, "Body": true} → true
      {"Type": 13, "Body": "2026…"}→ "2026…"
      {"Type": 17, "Body": {...}} → {...} (NodeId object)
      already-naked value → pass-through
    """
    if isinstance(encoded, dict) and "Type" in encoded and "Body" in encoded:
        body = encoded["Body"]
        # Recursively strip nested Variants (e.g. arrays of Variants).
        if isinstance(body, list):
            return [_strip_variant(item) for item in body]
        return body
    if isinstance(encoded, list):
        return [_strip_variant(item) for item in encoded]
    return encoded


def to_current_value(
    sample: ValueSample,
    *,
    is_composition: bool = False,
    components: dict[str, VQT] | None = None,
) -> CurrentValueResult:
    """Build a `CurrentValueResult` from a single ValueSample.

    Values are stripped of Part-6 Variant wrappers at this boundary so the
    i3X wire carries naked JSON values per SM Profile convention.
    """
    return CurrentValueResult(
        isComposition=is_composition,
        value=_strip_variant(sample.value),
        quality=sample.quality.value,
        timestamp=sample.timestamp or "",
        components=components,
    )


def to_composition_value(
    *,
    components: dict[str, VQT],
    quality: str,
    timestamp: str,
) -> CurrentValueResult:
    """Build a `CurrentValueResult` for a composition Object.

    Per implementation guide: `value` is `null` (not `{}`); the real data
    lives in `components` keyed by child **elementId** (not displayName).
    """
    return CurrentValueResult(
        isComposition=True,
        value=None,
        quality=quality,
        timestamp=timestamp,
        components=components if components else None,
    )


def to_empty_value(*, timestamp: str) -> CurrentValueResult:
    """Non-composition Object / Method — no value, no components."""
    return CurrentValueResult(
        isComposition=False,
        value={},
        quality=Quality.GoodNoData.value,
        timestamp=timestamp,
    )


def to_historical_value(
    element_id: str,
    samples: Iterable[ValueSample],
) -> HistoricalValueResult:
    _ = element_id  # retained for call-site readability
    samples_list = list(samples)
    return HistoricalValueResult(
        isComposition=False,
        values=[
            VQT(
                value=_strip_variant(s.value),
                quality=s.quality.value,
                timestamp=s.timestamp or "",
            )
            for s in samples_list
        ],
    )


# ------------------------------------------------------------------ quality


QUALITY_SYMBOL_MAP: dict[str, Quality] = {
    "Good": Quality.Good,
    "GoodNoData": Quality.GoodNoData,
    "Uncertain": Quality.GoodNoData,
}


def status_symbol_to_quality(symbol: str) -> Quality:
    if symbol in QUALITY_SYMBOL_MAP:
        return QUALITY_SYMBOL_MAP[symbol]
    if symbol.startswith("Bad"):
        return Quality.Bad
    if symbol.startswith("Uncertain"):
        return Quality.GoodNoData
    return Quality.GoodNoData


__all__ = [
    "QUALITY_SYMBOL_MAP",
    "status_symbol_to_quality",
    "to_composition_value",
    "to_current_value",
    "to_empty_value",
    "to_historical_value",
    "to_namespace",
    "to_namespaces",
    "to_object_instance",
    "to_object_instances",
    "to_object_type",
    "to_object_types",
]

_ = NodeClass
