"""GET /objecttypes, GET /objecttypes/{id}, POST /objecttypes/query."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status

from i3xua.api.deps import get_state, get_version, require_auth
from i3xua.api.state import AppState
from i3xua.api.versions import ApiVersion, shape_bulk, shape_single
from i3xua.core.mapping import to_object_type

router = APIRouter(dependencies=[Depends(require_auth)])


def _resolve_types(state: AppState, namespace_uri: str | None) -> list[dict[str, Any]]:
    out = []
    for entry in state.types.by_hash().values():
        ot = to_object_type(entry.descriptor)
        if namespace_uri is not None and ot.namespaceUri != namespace_uri:
            continue
        out.append(ot.model_dump(by_alias=True))
    return out


@router.get("/objecttypes")
async def list_object_types(
    state: Annotated[AppState, Depends(get_state)],
    version: Annotated[ApiVersion, Depends(get_version)],
    namespaceUri: Annotated[str | None, Query()] = None,
) -> object:
    return shape_single(version, _resolve_types(state, namespaceUri))


@router.get("/objecttypes/{element_id:path}")
async def get_object_type(
    element_id: str,
    state: Annotated[AppState, Depends(get_state)],
    version: Annotated[ApiVersion, Depends(get_version)],
) -> object:
    for entry in state.types.by_hash().values():
        ot = to_object_type(entry.descriptor)
        if ot.elementId == element_id:
            return shape_single(version, ot.model_dump(by_alias=True))
    raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown ObjectType: {element_id!r}")


@router.post("/objecttypes/query")
async def query_object_types(
    state: Annotated[AppState, Depends(get_state)],
    version: Annotated[ApiVersion, Depends(get_version)],
    body: Annotated[dict[str, Any], Body()],
) -> object:
    element_ids: list[str] = list(body.get("elementIds") or [])
    indexed = {ot_dict["elementId"]: ot_dict for ot_dict in _resolve_types(state, None)}
    results: list[dict[str, Any]] = []
    for eid in element_ids:
        ot = indexed.get(eid)
        if ot is None:
            results.append(
                {
                    "success": False,
                    "elementId": eid,
                    "error": {"code": 404, "message": f"Object type not found: {eid}"},
                }
            )
        else:
            results.append({"success": True, "elementId": eid, "result": ot})
    return shape_bulk(version, results)
