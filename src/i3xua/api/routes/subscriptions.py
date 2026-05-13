"""Subscription lifecycle + sync. SSE stream endpoint lands separately."""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from i3xua.api.deps import get_state, get_version, require_auth
from i3xua.api.sse import sse_stream
from i3xua.api.state import AppState
from i3xua.api.versions import ApiVersion, shape_single
from i3xua.core.neutral import ConnectionId, ElementRef

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_auth)])


async def _adapter_ref_change(
    state: AppState,
    added: list[str],
    removed: list[str],
) -> None:
    """Propagate refcount deltas to the upstream adapter.

    Element IDs are parsed for their connection prefix and grouped so each
    upstream call is one-per-connection. Failures in one connection don't
    block the others.
    """
    by_conn_add: dict[str, list[str]] = {}
    by_conn_rm: dict[str, list[str]] = {}
    for eid in added:
        try:
            ref = ElementRef.parse(eid)
        except ValueError:
            continue
        by_conn_add.setdefault(ref.connection, []).append(ref.node_id)
    for eid in removed:
        try:
            ref = ElementRef.parse(eid)
        except ValueError:
            continue
        by_conn_rm.setdefault(ref.connection, []).append(ref.node_id)

    # The upstream is responsible for mapping (connection, nodeIds) to its
    # internal SubscriptionHandle registry.
    from i3xua.core.neutral import SubscriptionHandle

    # Best-effort: if the OPC UA connection is down (mid-reconnect), the
    # SubscriptionManager still holds the registration. restoration
    # replays every wanted element when the connection comes back. Swallowing
    # the error here prevents a 500 from leaking to the i3X client, which
    # already got a 200 from the register path.
    for conn, nodes in by_conn_add.items():
        try:
            await state.upstream.add_monitored_items(
                SubscriptionHandle(connection=conn, subscription_name="default"),
                nodes,
            )
        except Exception as exc:
            logger.warning(
                "add_monitored_items(%s) failed (connection likely down): %s — "
                "restoration will retry on reconnect",
                conn,
                exc,
            )
    for conn, nodes in by_conn_rm.items():
        try:
            await state.upstream.remove_monitored_items(
                SubscriptionHandle(connection=conn, subscription_name="default"),
                nodes,
            )
        except Exception as exc:
            logger.warning("remove_monitored_items(%s) failed: %s", conn, exc)

    _ = ConnectionId  # keep the import used for symmetry with other routes


@router.post("/subscriptions")
async def create_subscription(
    state: Annotated[AppState, Depends(get_state)],
    version: Annotated[ApiVersion, Depends(get_version)],
    body: Annotated[dict[str, Any] | None, Body()] = None,
) -> object:
    sid = await state.subscriptions.create()
    return shape_single(version, {"subscriptionId": sid, "message": "Subscription created"})


@router.get("/subscriptions")
async def list_subscriptions(
    state: Annotated[AppState, Depends(get_state)],
    version: Annotated[ApiVersion, Depends(get_version)],
) -> object:
    # v0 surface only; v1 intentionally omits per i3X-Explorer contract.
    ids = await state.subscriptions.list_ids()
    return shape_single(version, [{"subscriptionId": sid, "created": ""} for sid in ids])


@router.post("/subscriptions/register")
async def register_v1(
    state: Annotated[AppState, Depends(get_state)],
    version: Annotated[ApiVersion, Depends(get_version)],
    body: Annotated[dict[str, Any], Body()],
) -> object:
    return await _register(
        state, version, body["subscriptionId"], list(body.get("elementIds") or [])
    )


@router.post("/subscriptions/{subscription_id}/register")
async def register_v0(
    subscription_id: str,
    state: Annotated[AppState, Depends(get_state)],
    version: Annotated[ApiVersion, Depends(get_version)],
    body: Annotated[dict[str, Any], Body()],
) -> object:
    return await _register(state, version, subscription_id, list(body.get("elementIds") or []))


async def _register(
    state: AppState, version: ApiVersion, subscription_id: str, element_ids: list[str]
) -> object:
    canonical = [_canonical_element_id(eid) for eid in element_ids]
    try:
        _, newly = await state.subscriptions.register(subscription_id, canonical)
    except KeyError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    await _adapter_ref_change(state, added=newly, removed=[])
    return shape_single(version, {"registered": len(canonical)})


def _canonical_element_id(element_id: str) -> str:
    try:
        return ElementRef.parse(element_id).as_id()
    except ValueError:
        return element_id


@router.post("/subscriptions/unregister")
async def unregister_v1(
    state: Annotated[AppState, Depends(get_state)],
    version: Annotated[ApiVersion, Depends(get_version)],
    body: Annotated[dict[str, Any], Body()],
) -> object:
    return await _unregister(
        state, version, body["subscriptionId"], list(body.get("elementIds") or [])
    )


@router.post("/subscriptions/{subscription_id}/unregister")
async def unregister_v0(
    subscription_id: str,
    state: Annotated[AppState, Depends(get_state)],
    version: Annotated[ApiVersion, Depends(get_version)],
    body: Annotated[dict[str, Any], Body()],
) -> object:
    return await _unregister(state, version, subscription_id, list(body.get("elementIds") or []))


async def _unregister(
    state: AppState, version: ApiVersion, subscription_id: str, element_ids: list[str]
) -> object:
    canonical = [_canonical_element_id(eid) for eid in element_ids]
    try:
        removed = await state.subscriptions.unregister(subscription_id, canonical)
    except KeyError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    await _adapter_ref_change(state, added=[], removed=removed)
    return shape_single(version, {"unregistered": len(removed)})


@router.post("/subscriptions/delete")
async def delete_v1(
    state: Annotated[AppState, Depends(get_state)],
    version: Annotated[ApiVersion, Depends(get_version)],
    body: Annotated[dict[str, Any], Body()],
) -> object:
    ids: list[str] = list(body.get("subscriptionIds") or [])
    for sid in ids:
        removed_elements = await state.subscriptions.delete(sid)
        await _adapter_ref_change(state, added=[], removed=list(removed_elements))
    return shape_single(version, {"deleted": len(ids)})


@router.delete("/subscriptions/{subscription_id}")
async def delete_v0(
    subscription_id: str,
    state: Annotated[AppState, Depends(get_state)],
    version: Annotated[ApiVersion, Depends(get_version)],
) -> object:
    removed_elements = await state.subscriptions.delete(subscription_id)
    await _adapter_ref_change(state, added=[], removed=list(removed_elements))
    return shape_single(version, {"deleted": 1})


@router.post("/subscriptions/stream")
async def stream_v1(
    request: Request,
    state: Annotated[AppState, Depends(get_state)],
    body: Annotated[dict[str, Any], Body()],
) -> StreamingResponse:
    sid: str = body["subscriptionId"]
    try:
        await state.subscriptions.get_elements(sid)  # validates existence
    except KeyError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    return StreamingResponse(
        sse_stream(request, state, sid),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/subscriptions/{subscription_id}/stream")
async def stream_v0(
    subscription_id: str,
    request: Request,
    state: Annotated[AppState, Depends(get_state)],
) -> StreamingResponse:
    try:
        await state.subscriptions.get_elements(subscription_id)
    except KeyError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    return StreamingResponse(
        sse_stream(request, state, subscription_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/subscriptions/sync")
async def sync_v1(
    state: Annotated[AppState, Depends(get_state)],
    body: Annotated[dict[str, Any], Body()],
) -> object:
    sid: str = body["subscriptionId"]
    last = body.get("lastSequenceNumber")
    try:
        result = await state.subscriptions.sync(
            sid, last_sequence_number=int(last) if last is not None else None
        )
    except KeyError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    items = [i.model_dump(by_alias=True) for i in result.items]
    return {"success": True, "result": items, "dropped": result.dropped}


@router.post("/subscriptions/{subscription_id}/sync")
async def sync_v0(
    subscription_id: str,
    state: Annotated[AppState, Depends(get_state)],
    body: Annotated[dict[str, Any] | None, Body()] = None,
) -> object:
    last = None
    if isinstance(body, dict):
        last = body.get("lastSequenceNumber")
    try:
        result = await state.subscriptions.sync(
            subscription_id,
            last_sequence_number=int(last) if last is not None else None,
        )
    except KeyError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    # v0 keyed shape
    v0: dict[str, Any] = {}
    for item in result.items:
        v0.setdefault(item.elementId, {"data": []})["data"].append(
            {"value": item.value, "quality": item.quality, "timestamp": item.timestamp}
        )
    return [v0] if v0 else []
