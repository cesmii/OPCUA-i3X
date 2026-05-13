"""POST /objects/value; PUT /objects/{id}/value -> 405 (;).

Value-read semantics by node class (CESMII RFC §3.2.3):

 - **Variable** → read upstream once; pass the Part-6-encoded VQT through.
 - **Composition Object** (has HasComponent children) → walk those children,
   read every leaf Variable in one batched upstream call, assemble into a
   `components` dict keyed by displayName. `isComposition` is intentionally
   omitted in v1 responses — clients infer composition from the presence of
   `components` (see i3X-Explorer `src/api/types.ts`).
 - **Non-composition Object** (folder-like, no HasComponent children) →
   empty-Bad placeholder with `quality="GoodNoData"` and current timestamp.

`maxDepth` (default 1) controls recursion through nested composition Objects.
`maxDepth=0` means infinite recursion; `maxDepth=1` descends one level
(i.e. surfaces the immediate component Variables, but leaves nested
composition children opaque).
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, status
from fastapi.responses import JSONResponse

from i3xua.api.deps import get_state, get_version, require_auth
from i3xua.api.state import AppState
from i3xua.api.versions import ApiVersion, shape_bulk, shape_single
from i3xua.core.mapping import (
    _strip_variant,
    to_composition_value,
    to_current_value,
    to_empty_value,
)
from i3xua.core.neutral import (
    ConnectionId,
    ElementRef,
    NodeClass,
    NodeDescriptor,
    Quality,
    ValueSample,
)
from i3xua.i3x.types import VQT, CurrentValueResult
from i3xua.ouajson import encode_datetime


def _dump_value_result(result: CurrentValueResult) -> dict[str, Any]:
    """Serialize per spec: `value` always present (even null),
    `components` absent when None (non-composition shouldn't carry it)."""
    d = result.model_dump(by_alias=True)
    if d.get("components") is None:
        d.pop("components", None)
    return d


logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_auth)])


def _now_iso() -> str:
    return encode_datetime(datetime.now(UTC))


async def _stale_lkv_fallback(state: AppState, element_id: str) -> ValueSample | None:
    """return a stale-LKV sample with quality=Bad and timestamp=now.

    Consulted when the upstream read yielded a `Quality.Bad` sample with no
    value — typically the reconnect window. If no history exists for the
    element, we surrender and let the caller emit the empty-Bad sample.
    """
    latest = await state.history.latest(element_id)
    if latest is None:
        return None
    return replace(latest, quality=Quality.Bad, timestamp=_now_iso())


def _is_empty_failure(sample: ValueSample) -> bool:
    return sample.quality is Quality.Bad and sample.value is None and not sample.timestamp


async def _read_with_fallback(
    state: AppState, conn: str, pairs: list[tuple[str, str]]
) -> list[ValueSample]:
    """Fetch a batch of values from the upstream, swapping in stale-LKV samples
    for any that came back as empty-Bad. If the whole upstream call
    itself fails — e.g. the connection thread is mid-reconnect and the
    ThreadPool bridge raised — fall back to stale LKV for every element in
    the batch."""
    node_ids = [p[1] for p in pairs]
    try:
        raw = await state.upstream.read_values(ConnectionId(conn), node_ids)
    except Exception as exc:
        logger.warning("upstream.read_values(%s) failed: %s; falling back to stale LKV", conn, exc)
        fallback: list[ValueSample] = []
        for eid, _nid in pairs:
            lkv = await _stale_lkv_fallback(state, eid)
            fallback.append(
                lkv
                or ValueSample(
                    element_id=eid,
                    value=None,
                    quality=Quality.Bad,
                    timestamp=_now_iso(),
                )
            )
        return fallback

    out: list[ValueSample] = []
    for (eid, _), sample in zip(pairs, raw, strict=True):
        if _is_empty_failure(sample):
            lkv = await _stale_lkv_fallback(state, eid)
            out.append(
                lkv
                or ValueSample(
                    element_id=eid,
                    value=None,
                    quality=Quality.Bad,
                    timestamp=_now_iso(),
                )
            )
        else:
            out.append(sample)
    return out


# ------------------------------------------------------------------ composition walk


def _direct_children(
    parent_element_id: str,
    snapshot: dict[str, NodeDescriptor],
    *,
    only_has_component: bool = True,
) -> list[NodeDescriptor]:
    """composition value rollup follows HasComponent only (RFC §3.2.3).
    Pass `only_has_component=False` for debug/tooling paths that want every
    hierarchical child.
    """
    parent = snapshot.get(parent_element_id)
    if parent is None:
        return []
    return [
        node
        for node in snapshot.values()
        if node.connection == parent.connection
        and node.parent_node_id == parent.node_id
        and (not only_has_component or node.parent_relationship == "HasComponent")
    ]


def _leaf_variable_ids(
    element_id: str,
    snapshot: dict[str, NodeDescriptor],
    *,
    max_depth: int,
    seen: set[str] | None = None,
) -> list[str]:
    """Collect every Variable reachable from a composition Object down to
    `max_depth` HasComponent levels. `max_depth=0` means infinite recursion;
    `max_depth=1` includes only direct children. Cycle-safe via `seen`.
    """
    seen = seen if seen is not None else set()
    if element_id in seen:
        return []
    seen.add(element_id)
    descriptor = snapshot.get(element_id)
    if descriptor is None:
        return []
    if descriptor.node_class is NodeClass.Variable:
        return [element_id]
    if not descriptor.is_composition:
        return []
    if max_depth == 1:
        out: list[str] = []
        for child in _direct_children(element_id, snapshot):
            if child.node_class is NodeClass.Variable:
                out.append(ElementRef(child.connection, child.node_id).as_id())
        return out
    next_depth = 0 if max_depth == 0 else max_depth - 1
    out = []
    for child in _direct_children(element_id, snapshot):
        child_eid = ElementRef(child.connection, child.node_id).as_id()
        out.extend(_leaf_variable_ids(child_eid, snapshot, max_depth=next_depth, seen=seen))
    return out


def _best_quality(qualities: list[str]) -> str:
    """Fold child qualities into the parent's — any Bad wins (worst-case)."""
    if not qualities:
        return Quality.GoodNoData.value
    if any(q == Quality.Bad.value for q in qualities):
        return Quality.Bad.value
    if any(q == Quality.GoodNoData.value for q in qualities):
        return Quality.GoodNoData.value
    return Quality.Good.value


@router.post("/objects/value")
async def read_values(
    state: Annotated[AppState, Depends(get_state)],
    version: Annotated[ApiVersion, Depends(get_version)],
    body: Annotated[dict[str, Any], Body()],
) -> object:
    element_ids: list[str] = list(body.get("elementIds") or [])
    max_depth: int = int(body.get("maxDepth", 1))
    snapshot = state.instances.snapshot()

    # ------ Phase 1: classify + enumerate leaf Variables to batch-read ------
    # category in {"variable", "composition", "empty", "unknown"}
    classifications: dict[str, tuple[NodeDescriptor | None, str]] = {}
    leaves_by_element: dict[str, list[str]] = {}
    leaf_ids_all: set[str] = set()

    for eid in element_ids:
        descriptor = snapshot.get(eid)
        if descriptor is None:
            classifications[eid] = (None, "unknown")
            continue
        if descriptor.node_class is NodeClass.Variable:
            classifications[eid] = (descriptor, "variable")
            leaves_by_element[eid] = [eid]
            leaf_ids_all.add(eid)
            continue
        if descriptor.is_composition:
            classifications[eid] = (descriptor, "composition")
            leaves = _leaf_variable_ids(eid, snapshot, max_depth=max_depth)
            leaves_by_element[eid] = leaves
            leaf_ids_all.update(leaves)
            continue
        classifications[eid] = (descriptor, "empty")

    # Unknown elements get a best-effort upstream read (treated as Variables).
    for eid, (_, cat) in classifications.items():
        if cat == "unknown":
            try:
                ElementRef.parse(eid)
            except ValueError:
                continue
            leaf_ids_all.add(eid)
            leaves_by_element[eid] = [eid]

    # ------ Phase 2: fetch every leaf in one batch per connection ------
    by_conn: dict[str, list[tuple[str, str]]] = {}
    for leaf_id in leaf_ids_all:
        try:
            ref = ElementRef.parse(leaf_id)
        except ValueError:
            continue
        by_conn.setdefault(ref.connection, []).append((leaf_id, ref.node_id))

    leaf_samples: dict[str, ValueSample] = {}
    for conn, pairs in by_conn.items():
        samples = await _read_with_fallback(state, conn, pairs)
        for (leaf_eid, _), sample in zip(pairs, samples, strict=True):
            leaf_samples[leaf_eid] = sample

    # ------ Phase 3: assemble per-element payload via mapping functions ------
    # Routes stay thin: classification + leaf enumeration + upstream fetch above;
    # the final wire shape comes ONLY from `core.mapping` functions.
    results: list[dict[str, Any]] = []
    for eid in element_ids:
        descriptor, cat = classifications[eid]

        if cat == "unknown":
            unknown_sample = leaf_samples.get(eid)
            if unknown_sample is None:
                results.append(
                    {
                        "success": False,
                        "elementId": eid,
                        "error": {"code": 404, "message": f"Element not found: {eid}"},
                    }
                )
                continue
            result = to_current_value(unknown_sample)
            results.append(
                {
                    "success": True,
                    "elementId": eid,
                    "result": _dump_value_result(result),
                }
            )
            continue

        if cat == "variable":
            sample = leaf_samples[eid]
            result = to_current_value(sample)
            results.append(
                {
                    "success": True,
                    "elementId": eid,
                    "result": _dump_value_result(result),
                }
            )
            continue

        if cat == "empty":
            result = to_empty_value(timestamp=_now_iso())
            results.append(
                {
                    "success": True,
                    "elementId": eid,
                    "result": _dump_value_result(result),
                }
            )
            continue

        # Composition: build VQT components from leaf samples.
        comp_vqts: dict[str, VQT] = {}
        latest_ts = ""
        qualities: list[str] = []
        for leaf_id in leaves_by_element.get(eid, []):
            leaf_sample = leaf_samples.get(leaf_id)
            leaf_descriptor = snapshot.get(leaf_id)
            if leaf_sample is None or leaf_descriptor is None:
                continue
            # Implementation guide: components keyed by elementId, not displayName.
            comp_vqts[leaf_id] = VQT(
                value=_strip_variant(leaf_sample.value),
                quality=leaf_sample.quality.value,
                timestamp=leaf_sample.timestamp or "",
            )
            qualities.append(leaf_sample.quality.value)
            if leaf_sample.timestamp and leaf_sample.timestamp > latest_ts:
                latest_ts = leaf_sample.timestamp
        result = to_composition_value(
            components=comp_vqts,
            quality=_best_quality(qualities),
            timestamp=latest_ts or _now_iso(),
        )
        results.append(
            {
                "success": True,
                "elementId": eid,
                "result": _dump_value_result(result),
            }
        )

    if version == "v1":
        return shape_bulk(version, results)
    v0: dict[str, Any] = {}
    for entry in results:
        if not entry["success"]:
            continue
        v0[entry["elementId"]] = {"data": [entry["result"]]}
    return shape_single(version, v0)


@router.put("/objects/{element_id:path}/value")
async def reject_write(element_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_405_METHOD_NOT_ALLOWED,
        content={
            "success": False,
            "error": {
                "code": 405,
                "message": "writes are disabled for v1.0. Re-enable via config writes.enabled=true.",
            },
        },
        headers={"Allow": "GET, POST"},
    )
