"""GET /namespaces."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from i3xua.api.deps import get_state, get_version, require_auth
from i3xua.api.state import AppState
from i3xua.api.versions import ApiVersion, shape_single
from i3xua.core.mapping import to_namespaces

router = APIRouter(dependencies=[Depends(require_auth)])


@router.get("/namespaces")
async def list_namespaces(
    state: Annotated[AppState, Depends(get_state)],
    version: Annotated[ApiVersion, Depends(get_version)],
) -> object:
    """Dedupe namespaces by displayName at the wire — first emission wins.

    The registry retains every per-connection entry (typed lookups still
    resolve via ``<uri>#connection=<name>`` keys); only the response list
    collapses duplicates.
    """
    infos = list(state.namespaces.snapshot().values())
    namespaces = to_namespaces(infos)
    seen: set[str] = set()
    deduped: list[dict[str, object]] = []
    for n in namespaces:
        if n.displayName in seen:
            continue
        seen.add(n.displayName)
        deduped.append(n.model_dump(by_alias=True))
    return shape_single(version, deduped)
