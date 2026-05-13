"""Server-facing Pydantic models: i3X types plus server-only response envelopes (e.g. /healthz)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from i3xua.i3x.types import (
    BatchResponse,
    BatchResult,
    CreateSubscriptionResponse,
    GetSubscriptionsResponse,
    HistoricalValue,
    LastKnownValue,
    Namespace,
    ObjectInstance,
    ObjectInstanceMinimal,
    ObjectType,
    RelationshipType,
    SubscriptionSummary,
    SyncResponseItem,
    V1BulkEnvelope,
    V1BulkItem,
    V1Envelope,
    VQTComponent,
)

__all__ = [
    "BatchResponse",
    "BatchResult",
    "CreateSubscriptionResponse",
    "GetSubscriptionsResponse",
    "HealthResponse",
    "HistoricalValue",
    "LastKnownValue",
    "Namespace",
    "ObjectInstance",
    "ObjectInstanceMinimal",
    "ObjectType",
    "RelationshipType",
    "SubscriptionSummary",
    "SyncResponseItem",
    "V1BulkEnvelope",
    "V1BulkItem",
    "V1Envelope",
    "VQTComponent",
]


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    version: str
    connections: dict[str, str]
