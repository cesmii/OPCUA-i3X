"""FastAPI dependencies: auth + versioned state lookup."""

from __future__ import annotations

import base64
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from i3xua.api.state import AppState
from i3xua.api.versions import ApiVersion
from i3xua.settings import BasicAuth, BearerAuth, NoneAuth


def get_state(request: Request) -> AppState:
    state: AppState = request.app.state.app_state
    return state


def get_version(request: Request) -> ApiVersion:
    """Determine which envelope shape to emit. Explicit `/v0` prefix wins; every
    other path — including the root-mount alias — emits v1 envelopes."""
    path = request.scope.get("root_path", "") + request.url.path
    if "/v0" in path:
        return "v0"
    return "v1"


def _authenticate(auth_cfg: BearerAuth | BasicAuth, header_value: str | None) -> None:
    if header_value is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing Authorization header")
    if isinstance(auth_cfg, BearerAuth):
        scheme, _, token = header_value.partition(" ")
        if scheme.lower() != "bearer" or token not in set(auth_cfg.tokens):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid bearer token")
        return
    # Basic
    scheme, _, payload = header_value.partition(" ")
    if scheme.lower() != "basic":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "expected basic auth")
    try:
        decoded = base64.b64decode(payload.encode()).decode()
    except Exception as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"malformed basic auth: {exc}") from None
    user, _, password = decoded.partition(":")
    for u in auth_cfg.users:
        if u.username == user and u.password == password:
            return
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")


def require_auth(
    state: Annotated[AppState, Depends(get_state)],
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> None:
    auth_cfg = state.config.server.auth
    if isinstance(auth_cfg, NoneAuth):
        return
    _authenticate(auth_cfg, authorization)


__all__ = ["get_state", "get_version", "require_auth"]
