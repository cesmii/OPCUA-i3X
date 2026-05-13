"""FastAPI app factory. Mounts every resource under `/`, `/v0`, and `/v1`.

Root-mount (no prefix) is an alias for the latest version — currently `v1`.
`get_version` in `deps.py` treats unprefixed paths as v1 so envelope shaping
stays consistent across all three surfaces.
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from i3xua import __version__
from i3xua.api.routes import (
    admin,
    history,
    namespaces,
    object_types,
    objects,
    subscriptions,
    values,
)
from i3xua.api.state import AppState


def _versioned_bundle() -> APIRouter:
    """Every resource route, including admin/info/healthz. Mounted at each
    configured version AND at the root so clients hitting `/namespaces` or
    `/v1/namespaces` resolve to the same handlers."""
    bundle = APIRouter()
    bundle.include_router(namespaces.router)
    bundle.include_router(object_types.router)
    bundle.include_router(objects.router)
    bundle.include_router(values.router)
    bundle.include_router(subscriptions.router)
    bundle.include_router(history.router)
    bundle.include_router(admin.router)
    return bundle


def build_app(state: AppState) -> FastAPI:
    app = FastAPI(
        title="i3xua",
        version=__version__,
        description="OPC UA 1.04 -> i3X v1.0 wrapper.",
    )
    app.state.app_state = state

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    for prefix in state.config.server.versions:
        app.include_router(_versioned_bundle(), prefix=f"/{prefix}")

    # Root alias: `/namespaces` etc. resolve identically to `/v1/namespaces`.
    # `deps.get_version` treats unprefixed paths as v1.
    app.include_router(_versioned_bundle())
    return app


__all__ = ["build_app"]
