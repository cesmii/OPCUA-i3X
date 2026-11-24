"""GET /relationshiptypes, POST /relationshiptypes/query (RFC 4.1.4/4.1.5).

Matches the i3X 1.0 release (api.i3x.dev/v1/openapi.json, 2026-11-24): the
i3X built-in pairs live in ``https://cesmii.org/i3x`` (HasParent/HasChildren,
HasComponent/ComponentOf, InheritedBy/InheritsFrom, exactly as the reference
server returns them). The other OPC UA hierarchical reference types the browse
walker stamps via ``RELATIONSHIP_NAMES`` (``sourceRelationship`` on
``/objects/related``) are listed under the OPC UA base namespace.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Query

from i3xua.api.deps import get_state, get_version, require_auth
from i3xua.api.state import AppState
from i3xua.api.versions import ApiVersion, shape_bulk, shape_single
from i3xua.core.mapping import to_namespaces
from i3xua.i3x.types import RelationshipType

router = APIRouter(dependencies=[Depends(require_auth)])

_UA_NS0 = "http://opcfoundation.org/UA/"
I3X_NS = "https://cesmii.org/i3x"

_I3X_BUILTINS: tuple[tuple[str, str], ...] = (
    ("HasParent", "HasChildren"),
    ("HasChildren", "HasParent"),
    ("HasComponent", "ComponentOf"),
    ("ComponentOf", "HasComponent"),
    ("InheritedBy", "InheritsFrom"),
    ("InheritsFrom", "InheritedBy"),
)

# OPC UA hierarchical refs beyond the i3X built-ins: (name, reverseOf, NodeId)
_UA_RELATIONSHIPS: tuple[tuple[str, str, str], ...] = (
    ("HasProperty", "PropertyOf", "i=46"),
    ("PropertyOf", "HasProperty", "i=46"),
    ("Organizes", "OrganizedBy", "i=35"),
    ("OrganizedBy", "Organizes", "i=35"),
    ("HasEventSource", "EventSourceOf", "i=36"),
    ("EventSourceOf", "HasEventSource", "i=36"),
    ("HasNotifier", "NotifierOf", "i=48"),
    ("NotifierOf", "HasNotifier", "i=48"),
    ("HasHistoricalConfiguration", "HistoricalConfigurationOf", "i=56"),
    ("HistoricalConfigurationOf", "HasHistoricalConfiguration", "i=56"),
)


def _ns0_uri(state: AppState) -> str:
    """The OPC UA base namespace URI as /namespaces emits it (connection-cast
    ``<uri>#connection=<name>`` when a connection has browsed, else bare)."""
    for ns in to_namespaces(list(state.namespaces.snapshot().values())):
        if ns.uri == _UA_NS0 or ns.uri.startswith(_UA_NS0 + "#"):
            return ns.uri
    return _UA_NS0


def _all(state: AppState) -> list[dict[str, Any]]:
    ua_ns = _ns0_uri(state)
    builtins = [
        RelationshipType(
            elementId=name,
            displayName=name,
            namespaceUri=I3X_NS,
            relationshipId=name,
            reverseOf=reverse,
        )
        for name, reverse in _I3X_BUILTINS
    ]
    ua = [
        RelationshipType(
            elementId=name,
            displayName=name,
            namespaceUri=ua_ns,
            relationshipId=ref,
            reverseOf=reverse,
        )
        for name, reverse, ref in _UA_RELATIONSHIPS
    ]
    return [r.model_dump(by_alias=True) for r in builtins + ua]


@router.get("/relationshiptypes")
async def list_relationship_types(
    state: Annotated[AppState, Depends(get_state)],
    version: Annotated[ApiVersion, Depends(get_version)],
    namespaceUri: Annotated[str | None, Query()] = None,
) -> object:
    out = _all(state)
    if namespaceUri is not None:
        out = [r for r in out if r["namespaceUri"] == namespaceUri]
    return shape_single(version, out)


@router.post("/relationshiptypes/query")
async def query_relationship_types(
    state: Annotated[AppState, Depends(get_state)],
    version: Annotated[ApiVersion, Depends(get_version)],
    body: Annotated[dict[str, Any], Body()],
) -> object:
    element_ids: list[str] = list(body.get("elementIds") or [])
    indexed = {r["elementId"]: r for r in _all(state)}
    results: list[dict[str, Any]] = []
    for eid in element_ids:
        if eid in indexed:
            results.append(
                {"success": True, "elementId": eid, "result": indexed[eid], "responseDetail": None}
            )
        else:
            results.append(
                {
                    "success": False,
                    "elementId": eid,
                    "result": None,
                    "responseDetail": {
                        "title": "Not Found",
                        "status": 404,
                        "detail": f"Relationship type not found: {eid}",
                    },
                }
            )
    return shape_bulk(version, results)
