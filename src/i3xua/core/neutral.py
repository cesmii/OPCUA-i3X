"""Neutral dataclasses that cross the hexagonal adapter boundary.

i3X core and the FastAPI layer consume these; only `adapters.asyncua` is allowed
to produce/translate them from OPC UA types. Keep this module free of Pydantic,
asyncua, and FastAPI imports so the Port stays pure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class NodeClass(StrEnum):
    Object = "Object"
    Variable = "Variable"
    Method = "Method"
    ObjectType = "ObjectType"
    VariableType = "VariableType"
    ReferenceType = "ReferenceType"
    DataType = "DataType"
    View = "View"


class Quality(StrEnum):
    Good = "Good"
    GoodNoData = "GoodNoData"
    Bad = "Bad"


@dataclass(frozen=True, slots=True)
class ConnectionId:
    name: str

    def __str__(self) -> str:
        return self.name


def canonicalize_node_id(raw: str) -> str:
    """Canonicalize an OPC UA NodeId string to asyncua's emit form.

    asyncua's `NodeId.to_string()` omits the `ns=0;` prefix for namespace 0.
    Our i3X clients may send either form; normalizing at the edges keeps the
    registries + subscription manager consistent with what arrives on the
    datachange path.
    """
    if not raw.startswith("ns="):
        return raw
    rest = raw[3:]
    ns_str, semi, tail = rest.partition(";")
    if not semi:
        return raw
    try:
        ns = int(ns_str)
    except ValueError:
        return raw
    if ns == 0:
        return tail
    return f"ns={ns};{tail}"


@dataclass(frozen=True, slots=True)
class ElementRef:
    """`<connectionName>!<canonical NodeId>` — the i3X elementId format.

    Construction normalizes the NodeId half to the short-form used by
    asyncua (`ns=0;` is stripped) so lookups and fan-out on the subscription
    path line up regardless of which form the client sent.
    """

    connection: str
    node_id: str

    def as_id(self) -> str:
        return f"{self.connection}!{self.node_id}"

    @classmethod
    def parse(cls, element_id: str) -> ElementRef:
        conn, sep, node = element_id.partition("!")
        if not sep:
            raise ValueError(f"invalid elementId: {element_id!r} (expected '<conn>!<nodeid>')")
        return cls(connection=conn, node_id=canonicalize_node_id(node))

    @classmethod
    def make(cls, connection: str, node_id: str) -> ElementRef:
        return cls(connection=connection, node_id=canonicalize_node_id(node_id))


@dataclass(frozen=True, slots=True)
class NamespaceInfo:
    """Per-connection wrapper-cast namespace.

    The wrapper casts ONE i3X namespace per connected OPC UA server. Its URI
    is computed from the connection name + the server's `ApplicationUri`.
    `uri` here is the ALREADY-CAST i3X URI — not the OPC UA NamespaceArray
    URI (which is a separate, internal concept tracked by NodeId namespace
    indices).
    """

    uri: str  # i3X namespace URI (wrapper-cast)
    connection: str  # which connection it came from
    display_name: str  # server's ApplicationName (fallback: connection name)
    application_uri: str | None = None  # server's ApplicationUri, kept for reference

    @property
    def i3x_uri(self) -> str:
        """The on-the-wire URI. After the wrapper-cast change, `uri` IS the
        i3X URI (already cast by `_build_namespaces`); this accessor stays for
        registry compat."""
        return self.uri


@dataclass(frozen=True, slots=True)
class TypeDescriptor:
    """An OPC UA ObjectType translated to something i3X can consume.

    `unresolved_fields` lists field names whose DataType couldn't be
    mapped to a concrete JSON Schema fragment — typically because the field
    references an abstract UA base (`Enumeration`, `Number`) or a custom
    DataType neither asyncua's codegen nor our walk could resolve. Instances
    of this type surface those fields via `isExtended=true` +
    `metadata.extendedAttributes` per CESMII RFC §3.2.
    """

    source_node_id: str  # stringified NodeId of the ObjectType
    display_name: str
    namespace_uri: str  # OPC UA namespace (the server's published URI for this type)
    connection: str
    structural_hash: str  # sha256(canonical(DataTypeDefinition))
    json_schema: dict[str, Any]
    version: str | None = None
    unresolved_fields: tuple[str, ...] = ()
    # Server's ApplicationUri, threaded from browse so the mapping layer can
    # compose the wrapper-cast i3X namespace URI without per-call lookups.
    application_uri: str | None = None


@dataclass(frozen=True, slots=True)
class NodeDescriptor:
    """An OPC UA Node (Object or Variable) translated for i3X.

    `parent_relationship` records the OPC UA reference type that led
    this node into the registry from its parent — one of `"HasComponent"`,
    `"HasProperty"`, `"Organizes"`, `"HasEventSource"`, `"HasNotifier"`,
    `"HasHistoricalConfiguration"`, or `""` for roots. Downstream
    `InstanceRegistry.children_of(...)` filters on this so the
    `/objects/related?relationshipType=...` route returns the requested slice;
    the post-walk `is_composition` sweep uses it to honor CESMII RFC §3.2.3
    (composition is HasComponent-scoped on Objects only).
    """

    node_id: str  # canonical OPC UA NodeId string
    connection: str
    display_name: str
    node_class: NodeClass
    namespace_uri: str
    type_source_id: str | None  # NodeId of TypeDefinition (Object) or DataType (Variable)
    parent_node_id: str | None
    is_composition: bool  # has HasComponent children (Object-scoped per)
    description: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    parent_relationship: str = ""  # ref-type name; "" for roots / unknown
    data_type_node_id: str | None = None  # Variables only; None for Objects/Methods
    # DataType BrowseName, resolved at browse time via either the Part-6
    # builtin table (`lookup_datatype_name`) or a one-shot batched Read of
    # BrowseName on unresolved DataType NodeIds (Duration, UtcTime, Decimal,
    # …). Mapping uses this for `metadata.system.dataTypeName` and the
    # artificial-type derivation prefers it over its own builtin lookup.
    data_type_name: str | None = None
    # Chosen NodeId-segment for the i3X elementId (``<conn>!<wire_node_id>``).
    # Numeric OPC UA NodeIds (``i=NN`` / ``ns=N;i=NN``) are opaque on the
    # wire, so the browse layer substitutes a ``<...>.<self.display_name>``
    # path (root segment dropped — already implicit in the connection prefix).
    # String/GUID/ByteString NodeIds keep their canonical form. Empty
    # ⇒ mapping falls back to ``node_id`` (descriptors built outside the
    # browse pipeline).
    wire_node_id: str = ""
    # Parent's `wire_node_id`, stamped in the same pass — lets
    # `InstanceRegistry.children_of` filter by parent's elementId without a
    # second registry lookup. Empty for roots.
    parent_wire_node_id: str = ""
    # Server's ApplicationUri, threaded from browse so the mapping layer can
    # compose the wrapper-cast i3X namespace URI without per-call lookups.
    application_uri: str | None = None
    # Variable attributes batched in a single multi-attribute Read at browse.
    # All None for non-Variable nodes; individual fields are also None when
    # the server returned ``BadAttributeIdInvalid`` (e.g. ``ArrayDimensions``
    # on scalars, ``AccessLevelEx`` on servers that don't expose it).
    access_level: int | None = None
    user_access_level: int | None = None
    value_rank: int | None = None
    array_dimensions: tuple[int, ...] | None = None
    historizing: bool | None = None
    minimum_sampling_interval: float | None = None


@dataclass(frozen=True, slots=True)
class ValueSample:
    """A datachange sample after Part-6 encoding.

    `value` is the OPC UA Part 6 reversible JSON encoding of the Variant body.
    """

    element_id: str  # "<conn>!<nodeid>"
    value: Any
    quality: Quality
    timestamp: str  # RFC 3339
    source_timestamp_ns: int | None = None
    server_timestamp_ns: int | None = None


@dataclass(frozen=True, slots=True)
class BrowseResult:
    namespaces: tuple[NamespaceInfo, ...]
    types: tuple[TypeDescriptor, ...]
    nodes: tuple[NodeDescriptor, ...]


@dataclass(frozen=True, slots=True)
class SubscriptionHandle:
    connection: str
    subscription_name: str  # matches config subscriptions[*].name
