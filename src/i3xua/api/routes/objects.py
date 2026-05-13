"""GET /objects, POST /objects/list, POST /objects/related."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status

from i3xua.api.deps import get_state, get_version, require_auth
from i3xua.api.state import AppState
from i3xua.api.versions import ApiVersion, shape_bulk, shape_single
from i3xua.core.mapping import to_object_instance
from i3xua.core.neutral import NodeDescriptor
from i3xua.i3x.types import ObjectInstance


def _dump_instance(inst: ObjectInstance) -> dict[str, Any]:
    """Serialize ObjectInstance per spec: exclude null fields (description,
    relationships, extendedAttributes, sourceRelationship) so the wire
    only carries populated data — matching the Implementation Guide examples.

    Required fields that MUST always be present (even when falsy/null):
    elementId, displayName, typeElementId, parentId, isComposition, isExtended.
    """
    d = inst.model_dump(by_alias=True, exclude_none=True)
    # Always include required fields even if their value is falsy.
    if "isComposition" not in d:
        d["isComposition"] = inst.isComposition
    if "isExtended" not in d:
        d["isExtended"] = inst.isExtended
    # parentId is required per spec — include even when null.
    if "parentId" not in d:
        d["parentId"] = inst.parentId
    return d


router = APIRouter(dependencies=[Depends(require_auth)])


def _all_instances(state: AppState) -> list[NodeDescriptor]:
    return list(state.instances.snapshot().values())


def _types_by_source(state: AppState) -> dict[str, Any]:
    """{source NodeId → TypeDescriptor} so `to_object_instance` can decide
    whether to set `isExtended=true` + `extendedAttributes` for this instance."""
    return {rt.descriptor.source_node_id: rt.descriptor for rt in state.types.by_hash().values()}


_GENERIC_PARENT_TYPES: frozenset[str] = frozenset({"i=58", "i=61"})
"""Mirrors `browse._GENERIC_PARENT_TYPES`. A Variable typed BaseDataVariableType
under one of these (BaseObjectType / FolderType) — or under a parentless / type-
less node — gets the artificial-type swap. Under any other parent type, the
parent already carries the semantic context and we pass through to the
server-reported type."""


def _build_instance(
    node: NodeDescriptor,
    state: AppState,
    types_map: dict[str, Any],
) -> ObjectInstance:
    """Build an ObjectInstance with relationships populated from the registry."""
    from i3xua.core.neutral import ElementRef

    eid = ElementRef(connection=node.connection, node_id=node.node_id).as_id()
    children = state.instances.children_of(eid)
    # Look up parent: display_name for human-readable relationships AND
    # type_source_id for the auto artificial-types decision.
    parent_display_name: str | None = None
    parent_type_source_id: str | None = None
    if node.parent_node_id is not None:
        parent_eid = ElementRef(connection=node.connection, node_id=node.parent_node_id).as_id()
        parent_node = state.instances.snapshot().get(parent_eid)
        if parent_node is not None:
            parent_display_name = parent_node.display_name
            parent_type_source_id = parent_node.type_source_id
    artificial_enabled = (
        parent_type_source_id is None or parent_type_source_id in _GENERIC_PARENT_TYPES
    )
    return to_object_instance(
        node,
        types=types_map,
        children=children,
        parent_display_name=parent_display_name,
        artificial_types_enabled=artificial_enabled,
    )


@router.get("/objects")
async def list_objects(
    state: Annotated[AppState, Depends(get_state)],
    version: Annotated[ApiVersion, Depends(get_version)],
    typeElementId: Annotated[str | None, Query()] = None,
    typeId: Annotated[str | None, Query()] = None,
    root: Annotated[bool, Query()] = False,
    includeMetadata: Annotated[bool, Query()] = False,
) -> object:
    wanted_type = typeElementId or typeId
    types_map = _types_by_source(state)
    out: list[dict[str, Any]] = []
    for node in _all_instances(state):
        inst = _build_instance(node, state, types_map)
        if wanted_type is not None and inst.typeElementId != wanted_type:
            continue
        if root and inst.parentId is not None:
            continue
        payload = _dump_instance(inst)
        if not includeMetadata:
            payload.pop("metadata", None)
        out.append(payload)
    return shape_single(version, out)


@router.get("/objects/{element_id:path}")
async def get_object(
    element_id: str,
    state: Annotated[AppState, Depends(get_state)],
    version: Annotated[ApiVersion, Depends(get_version)],
    includeMetadata: Annotated[bool, Query()] = False,
) -> object:
    descriptor = state.instances.snapshot().get(element_id)
    if descriptor is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown Object: {element_id!r}")
    payload = _dump_instance(_build_instance(descriptor, state, _types_by_source(state)))
    if not includeMetadata:
        payload.pop("metadata", None)
    return shape_single(version, payload)


@router.post("/objects/list")
async def list_objects_bulk(
    state: Annotated[AppState, Depends(get_state)],
    version: Annotated[ApiVersion, Depends(get_version)],
    body: Annotated[dict[str, Any], Body()],
) -> object:
    element_ids: list[str] = list(body.get("elementIds") or [])
    snap = state.instances.snapshot()
    types_map = _types_by_source(state)
    results: list[dict[str, Any]] = []
    for eid in element_ids:
        descriptor = snap.get(eid)
        if descriptor is None:
            results.append(
                {
                    "success": False,
                    "elementId": eid,
                    "error": {"code": 404, "message": f"Element not found: {eid}"},
                }
            )
        else:
            results.append(
                {
                    "success": True,
                    "elementId": eid,
                    "result": _dump_instance(_build_instance(descriptor, state, types_map)),
                }
            )
    return shape_bulk(version, results)


@router.post("/objects/related")
async def list_related(
    state: Annotated[AppState, Depends(get_state)],
    version: Annotated[ApiVersion, Depends(get_version)],
    body: Annotated[dict[str, Any], Body()],
) -> object:
    """`relationshipType` is a real filter now. Omitting it (or passing a
    falsy value) returns every hierarchical child regardless of ref type — that
    matches pre- behavior for unaware clients (like Explorer's tree expand).
    Passing `"HasComponent"` or `"HasProperty"` returns only that slice, and
    each returned envelope's `sourceRelationship` reports the child's actual
    ref type rather than echoing the request.
    """
    element_ids: list[str] = list(body.get("elementIds") or [])
    relationship_type = body.get("relationshipType") or body.get("relationshiptype") or None
    types_map = _types_by_source(state)
    results: list[dict[str, Any]] = []
    for eid in element_ids:
        children = state.instances.children_of(eid, relationship=relationship_type)
        envelope = [
            {
                "sourceRelationship": child.parent_relationship
                or (relationship_type or "HasComponent"),
                "object": _dump_instance(_build_instance(child, state, types_map)),
            }
            for child in children
        ]
        results.append({"success": True, "elementId": eid, "result": envelope})
    return shape_bulk(version, results)
