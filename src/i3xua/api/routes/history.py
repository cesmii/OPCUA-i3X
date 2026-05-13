"""POST /objects/history. PUT history -> 405."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, status
from fastapi.responses import JSONResponse

from i3xua.api.deps import get_state, get_version, require_auth
from i3xua.api.state import AppState
from i3xua.api.versions import ApiVersion, shape_bulk, shape_single
from i3xua.core.mapping import to_historical_value

router = APIRouter(dependencies=[Depends(require_auth)])


@router.post("/objects/history")
async def read_history(
    state: Annotated[AppState, Depends(get_state)],
    version: Annotated[ApiVersion, Depends(get_version)],
    body: Annotated[dict[str, Any], Body()],
) -> object:
    element_ids: list[str] = list(body.get("elementIds") or [])
    start = body.get("startTime")
    end = body.get("endTime")
    results: list[dict[str, Any]] = []
    for eid in element_ids:
        samples = await state.history.read(eid, start_time=start, end_time=end)
        hv = to_historical_value(eid, samples)
        results.append(
            {
                "success": True,
                "elementId": eid,
                "result": hv.model_dump(by_alias=True),
            }
        )
    if version == "v1":
        return shape_bulk(version, results)
    v0: dict[str, Any] = {}
    for entry in results:
        v0[entry["elementId"]] = {"data": entry["result"]["values"]}
    return shape_single(version, v0)


@router.put("/objects/{element_id:path}/history")
async def reject_history_write(element_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_405_METHOD_NOT_ALLOWED,
        content={
            "success": False,
            "error": {"code": 405, "message": "history writes are disabled for v1.0"},
        },
        headers={"Allow": "POST"},
    )
