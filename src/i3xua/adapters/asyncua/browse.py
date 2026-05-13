"""Address-space browse.

The algorithm is kept entirely off asyncua: it consumes a `BrowseSource`
Protocol that returns pre-digested `NodeInfo` / `TypeInfo` structures. The
production wrapper implements `BrowseSource` in terms of
`asyncua.Client.browse_nodes` + attribute reads.

This lets the walk (cycle detection, parent linking, hierarchical reference
filtering) be unit-tested against a small in-memory address space.
"""

from __future__ import annotations

import logging
import re
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

from i3xua.adapters.asyncua.typemap import datatype_to_json_schema, structure_to_json_schema
from i3xua.core.datatype_names import lookup_datatype_name
from i3xua.core.neutral import (
    BrowseResult,
    NamespaceInfo,
    NodeClass,
    NodeDescriptor,
    TypeDescriptor,
    canonicalize_node_id,
)
from i3xua.core.registry import type_structural_hash

logger = logging.getLogger(__name__)

# Progress log granularity: emit one INFO line per N instances appended to the
# walk output. Provides liveness during long browses.
_PROGRESS_EVERY = 1000

# Always emitted as the first entry in result.namespaces — even if the server's
# NamespaceArray omits it (impossible per spec, but defensive) and even if the
# user's `namespace_allowlist` excludes it. upstream.py relies on the invariant
# `result.namespaces[0].uri == UA_CORE_NAMESPACE_URI` for its `default_ns`
# fallback when loading types.
UA_CORE_NAMESPACE_URI = "http://opcfoundation.org/UA/"

# OPC UA reference-type NodeIds we traverse when building the instance tree.
# (Only HierarchicalReferences; never HasTypeDefinition / HasModellingRule.)
HIERARCHICAL_REFS: frozenset[str] = frozenset(
    # asyncua's `NodeId.to_string()` omits the `ns=0;` prefix for
    # namespace-0 NodeIds, so we accept both forms.
    {
        "ns=0;i=33",
        "i=33",  # HierarchicalReferences (base)
        "ns=0;i=35",
        "i=35",  # Organizes
        "ns=0;i=36",
        "i=36",  # HasEventSource
        "ns=0;i=47",
        "i=47",  # HasComponent
        "ns=0;i=46",
        "i=46",  # HasProperty
        "ns=0;i=48",
        "i=48",  # HasNotifier
        "ns=0;i=56",
        "i=56",  # HasHistoricalConfiguration
    }
)

HAS_COMPONENT_REFS: frozenset[str] = frozenset({"ns=0;i=47", "i=47"})
HAS_PROPERTY_REFS: frozenset[str] = frozenset({"ns=0;i=46", "i=46"})

# Object-only baseline. Variables/Methods are added when
# BrowseConfig.browse_variable_properties is True.
_BASEbrowseable: frozenset[NodeClass] = frozenset({NodeClass.Object})
_PROPERTYbrowseable: frozenset[NodeClass] = frozenset(
    {NodeClass.Object, NodeClass.Variable, NodeClass.Method}
)

# canonical relationship name stamped on `NodeDescriptor.parent_relationship`.
# asyncua emits namespace-0 NodeIds as `i=N` (no `ns=0;` prefix), so the lookup
# covers both forms. Unknown ref types (e.g. vendor-specific hierarchical subtypes)
# default to `""` in the walker.
RELATIONSHIP_NAMES: dict[str, str] = {
    "ns=0;i=47": "HasComponent",
    "i=47": "HasComponent",
    "ns=0;i=46": "HasProperty",
    "i=46": "HasProperty",
    "ns=0;i=35": "Organizes",
    "i=35": "Organizes",
    "ns=0;i=36": "HasEventSource",
    "i=36": "HasEventSource",
    "ns=0;i=48": "HasNotifier",
    "i=48": "HasNotifier",
    "ns=0;i=56": "HasHistoricalConfiguration",
    "i=56": "HasHistoricalConfiguration",
}

DEFAULT_INSTANCE_ROOTS: tuple[str, ...] = ("ns=0;i=85",)  # Objects folder
DEFAULT_TYPE_ROOTS: tuple[str, ...] = (
    "ns=0;i=58",  # BaseObjectType
    "ns=0;i=62",  # BaseVariableType
)


@dataclass(frozen=True, slots=True)
class NodeInfo:
    node_id: str
    browse_name: str
    display_name: str
    node_class: NodeClass
    namespace_uri: str
    data_type_node_id: str | None = None  # Variables only
    type_definition_node_id: str | None = None  # Objects: pointer to ObjectType
    parent_node_id: str | None = None
    description: str | None = None
    has_component_children: bool = False
    hierarchical_refs: tuple[tuple[str, str], ...] = ()  # (refType, targetNode)
    # Variable attributes from the upstream's batched multi-attribute Read.
    # None for non-Variable nodes or when the server returned BadAttributeIdInvalid.
    access_level: int | None = None
    user_access_level: int | None = None
    value_rank: int | None = None
    array_dimensions: tuple[int, ...] | None = None
    historizing: bool | None = None
    minimum_sampling_interval: float | None = None


@dataclass(frozen=True, slots=True)
class BrowsedChild:
    """A child returned by a parent's batched Browse with ResultMask=All.

    The `info` is fully populated from the BrowseResponse plus a single
    batched DataType read for Variable children. No further attribute
    reads are needed — re-browsing a child only happens if it can have
    children itself (i.e., NodeClass.Object).
    """

    info: NodeInfo
    parent_node_id: str
    parent_relationship: str  # canonical, e.g. "HasComponent" or ""


@dataclass(frozen=True, slots=True)
class InstanceDeclaration:
    name: str
    data_type_node_id: str
    value_rank: int = -1
    is_optional: bool = False  # ModellingRule = Optional


@dataclass(frozen=True, slots=True)
class TypeInfo:
    node_id: str
    display_name: str
    namespace_uri: str
    fields: tuple[InstanceDeclaration, ...]
    subtypes: tuple[str, ...] = ()
    version: str | None = None
    data_type_node_id: str | None = None  # VariableTypes only


class BrowseSource(Protocol):
    async def get_namespace_array(self) -> list[str]: ...

    async def get_application_uri(self) -> str | None:
        """Server's `ApplicationUri` from the chosen endpoint description.
        Used to compose the wrapper-cast i3X namespace URI for this connection.
        Implementations may return None when discovery hasn't run yet."""
        ...

    async def get_application_name(self) -> str | None:
        """Server's `ApplicationName.Text` from the chosen endpoint description.
        Used as the wrapper-cast namespace's displayName. Implementations may
        return None when discovery hasn't run yet — caller falls back to the
        connection name."""
        ...

    async def get_node_info(self, node_id: str) -> NodeInfo | None: ...

    async def get_node_info_batch(self, node_ids: list[str]) -> list[NodeInfo | None]:
        """Used only for the BFS roots (typically 1-3 NodeIds, off the hot
        path). Default falls back to per-node get_node_info."""
        ...

    async def browse_children(self, parent_node_ids: list[str]) -> list[BrowsedChild]:
        """One batched Browse(HierarchicalReferences, ResultMask=All) for
        the given parents, with CP draining and BadNoCP recovery, plus
        one batched DataType read for Variable children. Returns a flat
        list of fully-populated children across all parents (interleaved
        arbitrarily — each carries its own parent back-reference)."""
        ...

    async def get_type_info(self, node_id: str) -> TypeInfo | None: ...


# ------------------------------------------------------------------ helpers


_NUMERIC_NODE_ID_RE = re.compile(r"^(?:ns=\d+;)?i=\d+$")


def _stamp_wire_node_ids(nodes: list[NodeDescriptor]) -> list[NodeDescriptor]:
    """Compute `wire_node_id` and `parent_wire_node_id` per node.

    Rule: if the raw NodeId is opaque (numeric `i=N` / `ns=N;i=N`),
    substitute a `<...>.<display_name>` path; otherwise keep the canonical
    NodeId verbatim (string NodeIds like ``ns=2;s=Channel1.Tag1`` are
    already readable). The root segment is dropped from the path — it's
    implicit in the `<conn>!` prefix.

    Walks in BFS-emit order (browse always emits parents before children)
    so each child can read its parent's already-computed path/wire.
    """
    from dataclasses import replace as _dc_replace

    path_by_node: dict[str, str] = {}  # for descendants of the root
    wire_by_node: dict[str, str] = {}  # for parent_wire_node_id stamping
    seen_wires: set[str] = set()
    out: list[NodeDescriptor] = []
    for n in nodes:
        is_numeric = bool(_NUMERIC_NODE_ID_RE.match(n.node_id))

        # Build the path-down-from-root for descendants. Root itself
        # contributes nothing to children's paths (skip-root rule).
        if n.parent_node_id is None:
            path = ""  # root → no path needed; wire falls back to node_id
        else:
            parent_path = path_by_node.get(n.parent_node_id, "")
            path = f"{parent_path}.{n.display_name}" if parent_path else n.display_name
        path_by_node[n.node_id] = path

        wire = path if (is_numeric and path) else n.node_id
        # OPC UA only enforces BrowseName uniqueness among siblings;
        # DisplayName can repeat. When two raw NodeIds compute to the
        # same wire form, append the raw NodeId as a disambiguator so
        # the InstanceRegistry doesn't drop the duplicate via dict
        # overwrite. The result stays mostly readable
        # (`Boilers.Boiler #1.Value#ns=4;i=1248`).
        if wire in seen_wires:
            wire = f"{wire}#{n.node_id}"
        seen_wires.add(wire)
        parent_wire = wire_by_node.get(n.parent_node_id or "", "")
        wire_by_node[n.node_id] = wire

        out.append(_dc_replace(n, wire_node_id=wire, parent_wire_node_id=parent_wire))
    return out


async def _resolve_unresolved_datatype_names(
    source: BrowseSource, node_ids: list[str]
) -> dict[str, str]:
    """Read DisplayName for DataType NodeIds the Part-6 builtin table doesn't
    cover (Duration `i=290`, UtcTime `i=294`, LocaleId `i=295`, Decimal
    `i=17861`, etc.). One batched read; missing entries pass through to the
    `Custom_<sanitized-NodeId>` fallback.
    """
    if not node_ids:
        return {}
    infos = await source.get_node_info_batch(node_ids)
    return {
        nid: info.display_name
        for nid, info in zip(node_ids, infos, strict=True)
        if info is not None
    }


def _strip_uri_scheme(uri: str) -> str:
    """Build the displayName URI label: drop the scheme and the leading
    host-or-organization segment, keep the rest, swap `/` for `-`.

    For `http(s)://` URIs: drop scheme + domain, take everything after the
    first `/` past the domain (e.g. `http://opcfoundation.org/UA/Boiler/`
    → `UA-Boiler`).

    For `urn:` URIs: drop `urn:` and the first colon-separated segment
    (the host-or-org analogue), take what follows (e.g.
    `urn:i3xua:artificial-types` → `artificial-types`).
    """
    if uri.startswith("https://"):
        rest = uri[len("https://") :]
        slash = rest.find("/")
        rest = rest[slash + 1 :] if slash >= 0 else ""
    elif uri.startswith("http://"):
        rest = uri[len("http://") :]
        slash = rest.find("/")
        rest = rest[slash + 1 :] if slash >= 0 else ""
    elif uri.startswith("urn:"):
        rest = uri[len("urn:") :]
        colon = rest.find(":")
        rest = rest[colon + 1 :] if colon >= 0 else rest
    else:
        rest = uri
    return rest.rstrip("/#").replace("/", "-")


_GENERIC_PARENT_TYPES: frozenset[str] = frozenset({"i=58", "i=61"})
"""Parent type IDs that count as 'no meaningful context' for the
artificial-type auto rule. ``i=58`` BaseObjectType and ``i=61`` FolderType
— both generic UA placeholders. A Variable typed BaseDataVariableType under
one of these (or a parentless / type-less node) gets the artificial swap;
a Variable under a typed parent (BoilerType, AnalogItemType, ...) keeps
its server-reported type.
"""


def _eligible_for_artificial(
    node: NodeDescriptor, parent_type_by_node: dict[str, str | None]
) -> bool:
    """Per-instance artificial-swap decision."""
    from i3xua.core.artificial_types import should_replace as _should_replace

    if node.node_class != NodeClass.Variable:
        return False
    if not _should_replace(node.type_source_id):
        return False
    if node.data_type_node_id is None or node.value_rank is None or node.access_level is None:
        return False
    if node.parent_node_id is None:
        return True
    parent_type = parent_type_by_node.get(node.parent_node_id)
    return parent_type is None or parent_type in _GENERIC_PARENT_TYPES


def collect_artificial_types(
    nodes: list[NodeDescriptor],
    *,
    parent_type_by_node: dict[str, str | None] | None = None,
) -> list[TypeDescriptor]:
    """Lazy artificial-type registration.

    For every Variable that satisfies ``_eligible_for_artificial`` (server
    reports it as ``BaseDataVariableType`` AND its parent has no meaningful
    type), derive a (DataType, Rank, Access) shape and emit one
    ``TypeDescriptor`` per unique triple.

    ``parent_type_by_node`` is ``{parent_node_id: parent_type_source_id}``
    from the same browse cycle; when omitted, the parent check defaults to
    "generic" so all eligible Variables are collected.

    Descriptors live under each Variable's own connection + namespace.
    elementIds follow ``<connection>!<typename>``. Multiple Variables of the
    same shape from the same connection collapse onto a single descriptor;
    its ``namespace_uri`` is taken from the first such Variable.
    """
    from i3xua.adapters.asyncua.typemap import datatype_to_json_schema
    from i3xua.core.artificial_types import (
        artificial_type_name as _artificial_type_name,
    )
    from i3xua.core.artificial_types import (
        derive_shape as _derive_shape,
    )

    parents = parent_type_by_node if parent_type_by_node is not None else {}
    # Dedup key: (connection, shape). Same shape from two connections means
    # two TypeDescriptors — instances stay co-located with their server.
    seen: set[tuple[str, tuple[str, str, str]]] = set()
    out: list[TypeDescriptor] = []
    for node in nodes:
        if not _eligible_for_artificial(node, parents):
            continue
        assert node.data_type_node_id is not None
        assert node.value_rank is not None
        assert node.access_level is not None
        shape = _derive_shape(
            node.data_type_node_id,
            node.value_rank,
            node.access_level,
            dt_name_override=node.data_type_name,
        )
        key = (node.connection, shape)
        if key in seen:
            continue
        seen.add(key)
        name = _artificial_type_name(shape)
        value_schema = datatype_to_json_schema(node.data_type_node_id, value_rank=node.value_rank)
        out.append(
            TypeDescriptor(
                source_node_id=name,
                display_name=name,
                namespace_uri=node.namespace_uri,
                connection=node.connection,
                structural_hash=f"artificial:{node.connection}:{name}",
                json_schema={"type": "object", "properties": {"value": value_schema}},
            )
        )
    return out


def _build_namespaces(
    uris: Iterable[str],
    connection: str,
    *,
    application_name: str | None = None,
    application_uri: str | None = None,
) -> list[NamespaceInfo]:
    """Emit one i3X namespace per OPC UA NamespaceArray entry, 1:1.

    Each entry gets the per-connection collision suffix
    ``#connection=<name>`` so two servers publishing the same custom URI
    don't collide in the registry.

    Display format: ``<UriLabel>-ns<index>``. The full URI is preserved on
    ``NamespaceInfo.uri``.

    ``application_name`` and ``connection`` are accepted for API stability;
    neither participates in the displayName. ``application_uri`` is
    forwarded onto ``NamespaceInfo`` for downstream layers that key by it.
    """
    from urllib.parse import unquote

    _ = application_name  # retained for caller signature compatibility
    out: list[NamespaceInfo] = []
    for index, uri in enumerate(uris):
        decoded = unquote(uri).rstrip("/#")
        label = _strip_uri_scheme(decoded)
        display = f"{label}-ns{index}"
        out.append(
            NamespaceInfo(
                uri=f"{uri}#connection={connection}",
                connection=connection,
                display_name=display,
                application_uri=application_uri,
            )
        )
    return out


# ------------------------------------------------------------------ instance walk


def _adaptive_batch_size(
    queue_len: int,
    mean_children: float,
    max_children: int,
    max_parents: int,
    multiplier: float = 2.0,
) -> int:
    """Compute the next BFS batch size from observed payload.

    multiplier is a defensive factor against parent-size heterogeneity:
    the running mean lags by one batch, so when a fat parent appears
    after a sequence of small ones, the mean is small and we'd over-pack
    the first fat batch. multiplier=2 means we plan for "next batch's
    parents are 2x the mean we have so far." Self-correcting: once we've
    seen fat parents, the mean rises and the multiplier becomes a safety
    margin rather than the primary brake.

    Returns max(1, ...) so the batch is never empty even when
    mean_children is pathologically high.
    """
    estimated_per_parent = max(1.0, mean_children) * multiplier
    by_payload = max(1, int(max_children / estimated_per_parent))
    return min(queue_len, max_parents, by_payload)


# Hard cap on parents in one BrowseRequest. Server's
# OperationLimits.MaxNodesPerBrowse clamps this when published; otherwise
# the default applies. 1000 is permissive enough for most servers; the
# adaptive children-budget is the real backpressure.
MAX_PARENTS_PER_BATCH_DEFAULT = 1_000

# Soft target for total children returned across one batch. Primary
# backpressure against server response-size ceilings. Raise on servers with
# looser budgets, lower if Failed-to-send appears.
MAX_CHILDREN_PER_BATCH_DEFAULT = 50_000


async def _browse_instances(
    source: BrowseSource,
    connection: str,
    roots: Iterable[str],
    namespace_allowlist: frozenset[str] | None,
    *,
    max_parents_per_batch: int = MAX_PARENTS_PER_BATCH_DEFAULT,
    max_children_per_batch: int = MAX_CHILDREN_PER_BATCH_DEFAULT,
    browse_variable_properties: bool = False,
) -> list[NodeDescriptor]:
    """BFS over the instance tree.

    Strategy: each iteration calls source.browse_children(parents) which
    returns fully-populated BrowsedChild objects (NodeInfo built from the
    parent's BrowseResponse with ResultMask=All, plus a single batched
    DataType read for any Variable children).

    Children are emitted as NodeDescriptors immediately. Only children
    whose NodeClass is in the active browseable set are queued for further
    expansion — Variables, Methods, type nodes are leaves by default.

    The is_composition invariant is reconciled post-walk exactly
    as before, from parent_node_id + parent_relationship.

    ``browse_variable_properties`` controls whether Variable and Method
    nodes are queued for re-browsing (to discover HasProperty children
    like EURange, EngineeringUnits, TwoStateVariable.Id,
    Method.InputArguments). Default False — tag-heavy namespaces with
    hundreds of thousands of Variables don't typically expose meaningful
    HasProperty children and the extra round-trips are wasted.
    """
    browseable: frozenset[NodeClass] = (
        _PROPERTYbrowseable if browse_variable_properties else _BASEbrowseable
    )
    visited: set[str] = set()
    out: list[NodeDescriptor] = []
    next_progress_threshold = _PROGRESS_EVERY

    # ── Bootstrap: fetch root info one-off, off the hot path ──
    root_ids = [rid for rid in roots]
    root_infos = await source.get_node_info_batch(root_ids)
    queue: deque[str] = deque()
    for rid, info in zip(root_ids, root_infos, strict=True):
        if info is None:
            continue
        if rid in visited:
            continue
        visited.add(rid)
        if namespace_allowlist is not None and info.namespace_uri not in namespace_allowlist:
            # Root is filtered out — but its children might still be in the
            # allowed namespace, so we still need to enqueue if browseable.
            if info.node_class in browseable:
                queue.append(rid)
            continue
        out.append(
            NodeDescriptor(
                node_id=info.node_id,
                connection=connection,
                display_name=info.display_name,
                node_class=info.node_class,
                namespace_uri=info.namespace_uri,
                type_source_id=info.type_definition_node_id or info.data_type_node_id,
                data_type_node_id=info.data_type_node_id,
                parent_node_id=None,
                is_composition=info.has_component_children,
                description=info.description,
                parent_relationship="",
                access_level=info.access_level,
                user_access_level=info.user_access_level,
                value_rank=info.value_rank,
                array_dimensions=info.array_dimensions,
                historizing=info.historizing,
                minimum_sampling_interval=info.minimum_sampling_interval,
            )
        )
        if info.node_class in browseable:
            queue.append(rid)

    # ── BFS loop: browse_children handles all per-batch wire work ──
    # Running counters drive _adaptive_batch_size for the next pop. After
    # each browse_children call, accumulate observed children counts so
    # the next iteration's pack reflects the actual payload shape.
    total_children_seen: int = 0
    total_parents_browsed: int = 0
    while queue:
        mean = total_children_seen / max(1, total_parents_browsed)
        target = _adaptive_batch_size(
            queue_len=len(queue),
            mean_children=mean,
            max_children=max_children_per_batch,
            max_parents=max_parents_per_batch,
        )
        batch: list[str] = []
        while queue and len(batch) < target:
            batch.append(queue.popleft())
        if not batch:
            break

        children = await source.browse_children(batch)
        total_parents_browsed += len(batch)
        total_children_seen += len(children)

        for bc in children:
            child_nid = bc.info.node_id
            if child_nid in visited:
                continue
            visited.add(child_nid)
            if namespace_allowlist is not None and bc.info.namespace_uri not in namespace_allowlist:
                # Skip emission but still allow descent if browseable, so we
                # can find allowed-namespace descendants.
                if bc.info.node_class in browseable:
                    queue.append(child_nid)
                continue
            out.append(
                NodeDescriptor(
                    node_id=child_nid,
                    connection=connection,
                    display_name=bc.info.display_name,
                    node_class=bc.info.node_class,
                    namespace_uri=bc.info.namespace_uri,
                    type_source_id=bc.info.type_definition_node_id or bc.info.data_type_node_id,
                    data_type_node_id=bc.info.data_type_node_id,
                    parent_node_id=bc.parent_node_id,
                    is_composition=bc.info.has_component_children,
                    description=bc.info.description,
                    parent_relationship=bc.parent_relationship,
                    access_level=bc.info.access_level,
                    user_access_level=bc.info.user_access_level,
                    value_rank=bc.info.value_rank,
                    array_dimensions=bc.info.array_dimensions,
                    historizing=bc.info.historizing,
                    minimum_sampling_interval=bc.info.minimum_sampling_interval,
                )
            )
            if len(out) >= next_progress_threshold:
                logger.info(
                    "browse progress: %s — %d instances discovered (queue=%d, visited=%d)",
                    connection,
                    len(out),
                    len(queue),
                    len(visited),
                )
                next_progress_threshold += _PROGRESS_EVERY
            if bc.info.node_class in browseable:
                queue.append(child_nid)

    # ── post-walk reconciliation ──
    hascomp_parents: set[str] = {
        node.parent_node_id
        for node in out
        if node.parent_node_id is not None and node.parent_relationship == "HasComponent"
    }
    from dataclasses import replace as _replace

    return [
        _replace(
            node,
            is_composition=(
                node.node_class == NodeClass.Object and node.node_id in hascomp_parents
            ),
        )
        for node in out
    ]


# ------------------------------------------------------------------ type walk


async def _browse_types(
    source: BrowseSource,
    connection: str,
    roots: Iterable[str],
) -> list[TypeDescriptor]:
    queue: deque[str] = deque(roots)
    visited: set[str] = set()
    out: list[TypeDescriptor] = []
    while queue:
        tid = queue.popleft()
        if tid in visited:
            continue
        visited.add(tid)
        info = await source.get_type_info(tid)
        if info is None:
            continue
        fields = [(f.name, f.data_type_node_id, f.value_rank, f.is_optional) for f in info.fields]
        h = type_structural_hash(info.node_id, fields, version=info.version)
        schema = structure_to_json_schema(fields)
        if info.data_type_node_id is not None:
            value_schema = datatype_to_json_schema(info.data_type_node_id, value_rank=-1)
            properties = dict(schema.get("properties", {}))
            properties = {"value": value_schema, **properties}
            schema = {**schema, "properties": properties}
        out.append(
            TypeDescriptor(
                source_node_id=canonicalize_node_id(info.node_id),
                display_name=info.display_name,
                namespace_uri=info.namespace_uri,
                connection=connection,
                structural_hash=h,
                json_schema=schema,
                version=info.version,
            )
        )
        queue.extend(info.subtypes)
    return out


# ------------------------------------------------------------------ public API


@dataclass(frozen=True, slots=True)
class BrowseConfig:
    instance_roots: tuple[str, ...] = DEFAULT_INSTANCE_ROOTS
    type_roots: tuple[str, ...] = DEFAULT_TYPE_ROOTS
    namespace_allowlist: tuple[str, ...] = field(default_factory=tuple)
    # Hard cap on parents per BrowseRequest. Server's
    # OperationLimits.MaxNodesPerBrowse may clamp this further at connect
    # time when published.
    max_parents_per_batch: int = MAX_PARENTS_PER_BATCH_DEFAULT
    # Soft children budget. Walker tracks a running mean of
    # children-per-parent and packs batches until estimated payload
    # (mean x 2.0 multiplier x parent_count) approaches this number.
    max_children_per_batch: int = MAX_CHILDREN_PER_BATCH_DEFAULT
    # OPC UA exposes Variable/Method metadata in two places: node
    # ATTRIBUTES (fetched via Read) and HasProperty CHILDREN (discovered
    # via Browse). Tag-heavy SCADA servers expose ATTRIBUTES only;
    # descending into Variables costs round-trips for zero new content.
    # Set True for servers that publish HasProperty children (e.g. servers
    # using EURange, Method.InputArguments, TwoStateVariable.Id,
    # Alarms/Conditions, generic OPC UA test servers).
    browse_variable_properties: bool = False


async def browse(
    source: BrowseSource,
    connection: str,
    *,
    cfg: BrowseConfig | None = None,
) -> BrowseResult:
    cfg = cfg or BrowseConfig()
    allow = frozenset(cfg.namespace_allowlist) if cfg.namespace_allowlist else None

    ns_array = await source.get_namespace_array()

    # Walk instances and types so descriptors get stamped before we return.
    nodes = await _browse_instances(
        source,
        connection,
        cfg.instance_roots,
        allow,
        max_parents_per_batch=cfg.max_parents_per_batch,
        max_children_per_batch=cfg.max_children_per_batch,
        browse_variable_properties=cfg.browse_variable_properties,
    )
    types = await _browse_types(source, connection, cfg.type_roots)

    # Build the exposed-URI list: ns=0 first (default_ns invariant — see
    # UA_CORE_NAMESPACE_URI doc), then the rest of NamespaceArray with the
    # allowlist applied. ns=0 is never filtered: the upstream layer falls
    # back to `result.namespaces[0]` when loading types from the UA core.
    exposed: list[str] = []
    seen: set[str] = set()
    exposed.append(UA_CORE_NAMESPACE_URI)
    seen.add(UA_CORE_NAMESPACE_URI)
    for uri in ns_array:
        if uri in seen:
            continue
        if allow is not None and uri != UA_CORE_NAMESPACE_URI and uri not in allow:
            continue
        exposed.append(uri)
        seen.add(uri)

    # ApplicationUri/Name looked up via getattr so older BrowseSource fakes
    # without these methods still work; the production adapter always provides them.
    get_app_uri = getattr(source, "get_application_uri", None)
    get_app_name = getattr(source, "get_application_name", None)
    application_uri = await get_app_uri() if get_app_uri is not None else None
    application_name = await get_app_name() if get_app_name is not None else None
    namespaces = _build_namespaces(
        exposed,
        connection,
        application_name=application_name,
        application_uri=application_uri,
    )

    # Stamp ApplicationUri onto each emitted descriptor so the mapping
    # layer can compose the per-connection collision URI without per-call
    # lookups. Also rename hierarchy roots (parent_node_id is None) to the
    # server's ApplicationName so the tree displays the server identity
    # instead of the generic "Objects" folder name.
    from dataclasses import replace as _dc_replace

    if application_uri is not None:
        types = [_dc_replace(t, application_uri=application_uri) for t in types]
        nodes = [_dc_replace(n, application_uri=application_uri) for n in nodes]

    if application_name:
        nodes = [
            _dc_replace(n, display_name=application_name) if n.parent_node_id is None else n
            for n in nodes
        ]

    # Compute `wire_node_id` for each node so the i3X elementId is
    # `<conn>!<display.path>` for opaque numeric NodeIds (`ns=5;i=1240` →
    # `Boilers.Boiler1.Pipe1001`) and `<conn>!<canonical-NodeId>` for
    # already-readable string NodeIds. Must run AFTER the root rename
    # above so the path's leading segment is the renamed ApplicationName.
    nodes = _stamp_wire_node_ids(nodes)

    # Resolve DataType BrowseNames beyond the Part-6 builtin table (Duration,
    # UtcTime, LocaleId, Decimal, …) via one batched read on the underlying
    # source. The resolved names get stamped onto each NodeDescriptor's
    # `data_type_name` so both `metadata.system.dataTypeName` (mapping) and
    # the artificial-type derivation (collect_artificial_types) prefer the
    # real name over the `Custom_<sanitized>` fallback.
    unresolved_dt_ids = sorted(
        {
            n.data_type_node_id
            for n in nodes
            if n.data_type_node_id is not None and lookup_datatype_name(n.data_type_node_id) is None
        }
    )
    extra_dt_names = await _resolve_unresolved_datatype_names(source, unresolved_dt_ids)
    if extra_dt_names:
        nodes = [
            _dc_replace(
                n,
                data_type_name=(
                    lookup_datatype_name(n.data_type_node_id)
                    or extra_dt_names.get(n.data_type_node_id or "")
                    if n.data_type_node_id
                    else None
                ),
            )
            for n in nodes
        ]

    # Lazy artificial-type registration. Walks `nodes` for generic-typed
    # Variables under generic parents (FolderType, BaseObjectType, or no
    # type), derives the (DataType x Rank x Access) shape, and emits one
    # TypeDescriptor per unique triple co-located with the source connection.
    # Variables under typed parents keep their server-reported type.
    parent_type_by_node: dict[str, str | None] = {n.node_id: n.type_source_id for n in nodes}
    artificial = collect_artificial_types(list(nodes), parent_type_by_node=parent_type_by_node)
    if artificial:
        # Artificial descriptors carry connection + namespace_uri taken
        # from their source Variables, so they fold into the existing
        # namespaces — no synthetic namespace to inject.
        types = list(types) + artificial

    # `!` is RESERVED as the elementId separator (`<conn>!<node_id>`).
    # Sanitize every server-supplied string we'll round-trip through an
    # elementId or surface in a displayName: replace `!` with `-`. NodeIds,
    # display names, namespace URIs (rare but possible), ApplicationName
    # text — all run through this pass before reaching the registry.
    namespaces = [
        _dc_replace(
            ns,
            uri=ns.uri.replace("!", "-"),
            display_name=ns.display_name.replace("!", "-"),
        )
        for ns in namespaces
    ]
    types = [
        _dc_replace(
            t,
            source_node_id=t.source_node_id.replace("!", "-"),
            display_name=t.display_name.replace("!", "-"),
            namespace_uri=t.namespace_uri.replace("!", "-"),
        )
        for t in types
    ]
    nodes = [
        _dc_replace(
            n,
            node_id=n.node_id.replace("!", "-"),
            display_name=n.display_name.replace("!", "-"),
            namespace_uri=n.namespace_uri.replace("!", "-"),
            parent_node_id=(
                n.parent_node_id.replace("!", "-") if n.parent_node_id is not None else None
            ),
            type_source_id=(
                n.type_source_id.replace("!", "-") if n.type_source_id is not None else None
            ),
        )
        for n in nodes
    ]

    return BrowseResult(
        namespaces=tuple(namespaces),
        types=tuple(types),
        nodes=tuple(nodes),
    )


# ------------------------------------------------------------------ type hints


__all__ = [
    "HIERARCHICAL_REFS",
    "BrowseConfig",
    "BrowseSource",
    "BrowsedChild",
    "InstanceDeclaration",
    "NodeInfo",
    "TypeInfo",
    "browse",
]

# Silence unused-import warnings for Any (used in callers' Protocol impls).
_ = Any
