"""Contract tests for the /admin/andon/* endpoints."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from i3xua.api.state import AppState

TOKEN_HEADER = {"Authorization": "Bearer test-token"}


@pytest.mark.contract
async def test_admin_andon_regenerate_requires_auth(
    http_client: httpx.AsyncClient,
) -> None:
    """POST /admin/andon/regenerate must sit behind bearer auth like the rest of /admin."""
    resp = await http_client.post("/v1/admin/andon/regenerate")
    assert resp.status_code == 401


@pytest.mark.contract
async def test_admin_andon_regenerate_concurrent_returns_409(
    http_client: httpx.AsyncClient, app_state: AppState
) -> None:
    """While a regen subprocess is running, a second request returns 409.

    We patch the subprocess invocation with a coroutine that holds the lock
    long enough to fire two parallel requests. The second must come back
    409 immediately without blocking.
    """

    async def _slow_regen(*_: Any, **__: Any) -> dict[str, Any]:
        await asyncio.sleep(0.5)
        return {"ok": True, "duration_s": 0.5, "generated_at": "test"}

    with patch(
        "i3xua.api.routes.admin._run_andon_subprocess",
        new=_slow_regen,
    ):
        first_task = asyncio.create_task(
            http_client.post("/v1/admin/andon/regenerate", headers=TOKEN_HEADER)
        )
        # Give the first request a beat to acquire the lock.
        await asyncio.sleep(0.05)
        second_resp = await http_client.post("/v1/admin/andon/regenerate", headers=TOKEN_HEADER)
        first_resp = await first_task

    assert first_resp.status_code == 200, f"first should succeed: {first_resp.text}"
    assert second_resp.status_code == 409
    assert second_resp.json() == {"error": "regen in progress"}


@pytest.mark.contract
async def test_admin_andon_report_requires_auth(
    http_client: httpx.AsyncClient,
) -> None:
    """GET /admin/andon/report must sit behind bearer auth."""
    resp = await http_client.get("/v1/admin/andon/report")
    assert resp.status_code == 401


@pytest.mark.contract
async def test_admin_andon_report_returns_404_when_no_file(
    http_client: httpx.AsyncClient, tmp_path: Any, monkeypatch: Any
) -> None:
    """When andon-report.html does not exist, the route returns 404 with
    an HTML body containing a hint to click the regenerate button."""
    # Point _repo_root at a tmp dir with no andon-report.html. The 404
    # path triggers when the file is missing.
    (tmp_path / "pyproject.toml").write_text("[project]\nname='fake'\n")
    monkeypatch.setattr("i3xua.api.routes.admin._repo_root", lambda start=None: tmp_path)
    resp = await http_client.get("/v1/admin/andon/report", headers=TOKEN_HEADER)
    assert resp.status_code == 404
    assert "text/html" in resp.headers.get("content-type", "")
    assert "Run health check" in resp.text or "regenerate" in resp.text.lower()
