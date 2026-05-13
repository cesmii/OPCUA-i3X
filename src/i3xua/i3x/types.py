"""i3X wire-contract types — the canonical response shapes.

These models define the EXACT JSON structure i3X clients expect. They are
the highest-afferent module in the codebase: routes, mapping, client, and
tests all depend on them. Changes here cascade everywhere — match the
reference i3X server implementation exactly.

Source of truth: i3X RFC v1.0-Beta.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _I3XBase(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        extra="allow",
        str_strip_whitespace=False,
    )


# ------------------------------------------------------------------ discovery


class Namespace(_I3XBase):
    """RFC 4.1.1."""

    uri: str
    displayName: str


class ObjectType(_I3XBase):
    """RFC 4.1.2/4.1.3 — ObjectTypeResponse shape from the reference server."""

    elementId: str
    displayName: str
    namespaceUri: str
    sourceTypeId: str = Field(
        ...,
        description="Class or member of the Namespace that defines this type",
    )
    version: str | None = None
    schema_: dict[str, Any] = Field(
        default_factory=dict,
        alias="schema",
        description="JSON Schema definition for this object type",
    )
    related: dict[str, Any] | None = None


class RelationshipType(_I3XBase):
    """RFC 4.1.4/4.1.5."""

    elementId: str
    displayName: str
    namespaceUri: str
    relationshipId: str = Field(
        ...,
        description="Class or member of the Namespace that defines this relationship type",
    )
    reverseOf: str


# ------------------------------------------------------------------ instances


class ObjectInstanceMetadata(_I3XBase):
    """Structured metadata block for v1 instance responses.

    Replaces the raw `dict` we used before — the reference server emits
    specific named fields, and i3X-Explorer reads them by key.
    """

    typeNamespaceUri: str | None = None
    sourceTypeId: str | None = None
    dataType: str | None = None
    dataTypeName: str | None = None
    description: str | None = None
    extendedAttributes: dict[str, Any] | None = None
    system: dict[str, Any] | None = None


class ObjectInstanceMinimal(_I3XBase):
    """RFC 3.1.1 — reference uses `typeElementId` (not `typeId`).

    Spec: 'Object instances do not belong to a Namespace; they exist in
    the server's implicit address space.' The required fields are exactly:
    elementId, displayName, typeElementId, parentId, isComposition, isExtended.
    `namespaceUri` is NOT on the instance — it lives in
    `metadata.typeNamespaceUri` when `includeMetadata=true`.
    """

    elementId: str
    displayName: str
    typeElementId: str = Field(..., description="ElementId of the object type")
    parentId: str | None = None
    isComposition: bool


class ObjectInstance(ObjectInstanceMinimal):
    """RFC 3.1.1 + 3.1.2 — full response with metadata.

    `relationships` is a top-level field per the Understanding Relationships
    doc — every spec example shows it next to elementId/parentId/isComposition.
    """

    isExtended: bool = False
    namespaceUri: str | None = None
    relationships: dict[str, Any] | None = None
    metadata: ObjectInstanceMetadata | None = None
    # Populated by POST /objects/related (v1 envelope field)
    sourceRelationship: str | None = None


# ------------------------------------------------------------------ values


class VQT(_I3XBase):
    """Value-Quality-Timestamp triplet."""

    value: Any
    quality: str = Field(
        ...,
        description="Data quality indicator: Good, GoodNoData, Bad",
    )
    timestamp: str = Field(
        ...,
        description="RFC 3339 UTC timestamp when this value was recorded",
    )


class CurrentValueResult(_I3XBase):
    """Response shape for `POST /objects/value` bulk result items.

    `isComposition` is present (the reference server emits it). `components`
    is populated only when `isComposition=True` and `maxDepth > 0`.
    """

    isComposition: bool = Field(
        False,
        description="True if this Object encapsulates composed child elements",
    )
    value: Any = None
    quality: str = "GoodNoData"
    timestamp: str = ""
    components: dict[str, VQT] | None = None


class HistoricalValueResult(_I3XBase):
    """Response shape for `POST /objects/history` bulk result items."""

    isComposition: bool = False
    values: list[VQT] = Field(default_factory=list)


# ------------------------------------------------------------------ subscriptions


class CreateSubscriptionRequest(_I3XBase):
    clientId: str | None = None
    displayName: str | None = None


class CreateSubscriptionResponse(_I3XBase):
    clientId: str | None = None
    subscriptionId: str
    displayName: str | None = None


class SyncResponseItem(_I3XBase):
    """Individual item in a sync or SSE batch.

    Fields default to safe values so the i3X client can construct partial items
    during v0 parsing where not all fields are present.
    """

    sequenceNumber: int = 0
    elementId: str = ""
    value: Any = None
    quality: str = "GoodNoData"
    timestamp: str = ""


class SubscriptionDetail(_I3XBase):
    subscriptionId: str
    displayName: str | None = None
    monitoredObjects: list[dict[str, Any]] = Field(default_factory=list)


# ------------------------------------------------------------------ admin


class _QueryCapabilities(_I3XBase):
    history: bool


class _UpdateCapabilities(_I3XBase):
    current: bool
    history: bool


class _SubscribeCapabilities(_I3XBase):
    stream: bool


class ServerCapabilities(_I3XBase):
    query: _QueryCapabilities
    update: _UpdateCapabilities
    subscribe: _SubscribeCapabilities


class ServerInfo(_I3XBase):
    """Response for `GET /info`."""

    specVersion: str = "1.0"
    serverVersion: str | None = None
    serverName: str | None = None
    capabilities: ServerCapabilities = Field(
        default_factory=lambda: ServerCapabilities(
            query=_QueryCapabilities(history=True),
            update=_UpdateCapabilities(current=False, history=False),
            subscribe=_SubscribeCapabilities(stream=True),
        )
    )


# ------------------------------------------------------------------ generic wrappers


class ErrorDetail(_I3XBase):
    code: int
    message: str


class BulkResultItem(_I3XBase):
    success: bool
    elementId: str | None = None
    subscriptionId: str | None = None
    result: Any = None
    error: ErrorDetail | None = None


class BulkResponse(_I3XBase):
    success: bool
    results: list[BulkResultItem] = Field(default_factory=list)


class SuccessResponse(_I3XBase):
    success: bool
    result: Any = None


# ------------------------------------------------------------------ legacy compat (client + v0 paths)

# Aliases for the Python i3X client and older tests. The canonical shapes
# above are what the wire uses; these prevent import breakage.
LastKnownValue = CurrentValueResult
HistoricalValue = HistoricalValueResult
VQTComponent = VQT


class SubscriptionSummary(_I3XBase):
    """v0 list-subscriptions shape."""

    subscriptionId: int
    created: str


class GetSubscriptionsResponse(_I3XBase):
    """v0 list-subscriptions response."""

    subscriptionIds: list[SubscriptionSummary] = Field(default_factory=list)


class BatchResult(_I3XBase):
    """Legacy v0 bulk-result item."""

    elementId: str
    success: bool
    data: Any = None
    error: str | None = None


class BatchResponse(_I3XBase):
    """Legacy v0 bulk response."""

    results: list[BatchResult] = Field(default_factory=list)
    totalRequested: int = 0
    totalSuccess: int = 0
    totalFailed: int = 0


# v1 envelope shapes used by the client's _request unwrapper.
class V1Envelope(_I3XBase):
    success: bool
    result: Any = None
    message: str | None = None


class V1BulkItem(_I3XBase):
    success: bool
    elementId: str
    result: Any = None
    error: str | None = None


class V1BulkEnvelope(_I3XBase):
    success: bool
    results: list[V1BulkItem] = Field(default_factory=list)
