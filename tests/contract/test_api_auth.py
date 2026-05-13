"""Auth contract: bearer + none + basic + rejection cases."""

from __future__ import annotations

import base64

import httpx
import pytest
from httpx import ASGITransport

from i3xua.api.app_factory import build_app
from i3xua.api.state import AppState, build_state
from i3xua.settings import (
    AppConfig,
    BasicAuth,
    BasicUser,
    BearerAuth,
    ConnectionConfig,
    NoneAuth,
    ServerConfig,
)
from tests.conftest import FakeUpstream


def _config(auth) -> AppConfig:
    return AppConfig(
        server=ServerConfig(auth=auth),
        connections=[
            ConnectionConfig(
                name="c",
                endpoint="opc.tcp://x:1",
            )
        ],
    )


async def _client(app_config: AppConfig) -> tuple[httpx.AsyncClient, AppState]:
    state = build_state(app_config, upstream=FakeUpstream(browse={}))  # type: ignore[arg-type]
    app = build_app(state)
    return (
        httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://i3x.test"),
        state,
    )


@pytest.mark.contract
async def test_bearer_missing_token_returns_401() -> None:
    client, _ = await _client(_config(BearerAuth(mode="bearer", tokens=["ok"])))
    async with client:
        resp = await client.get("/v1/namespaces")
        assert resp.status_code == 401


@pytest.mark.contract
async def test_bearer_wrong_token_returns_401() -> None:
    client, _ = await _client(_config(BearerAuth(mode="bearer", tokens=["ok"])))
    async with client:
        resp = await client.get("/v1/namespaces", headers={"Authorization": "Bearer wrong"})
        assert resp.status_code == 401


@pytest.mark.contract
async def test_bearer_correct_token_passes() -> None:
    client, _ = await _client(_config(BearerAuth(mode="bearer", tokens=["ok"])))
    async with client:
        resp = await client.get("/v1/namespaces", headers={"Authorization": "Bearer ok"})
        assert resp.status_code == 200


@pytest.mark.contract
async def test_basic_auth_success_and_failure() -> None:
    client, _ = await _client(
        _config(BasicAuth(mode="basic", users=[BasicUser(username="u", password="p")]))
    )
    good = base64.b64encode(b"u:p").decode()
    bad = base64.b64encode(b"u:wrong").decode()
    async with client:
        ok = await client.get("/v1/namespaces", headers={"Authorization": f"Basic {good}"})
        fail = await client.get("/v1/namespaces", headers={"Authorization": f"Basic {bad}"})
        assert ok.status_code == 200
        assert fail.status_code == 401


@pytest.mark.contract
async def test_none_auth_allows_any_request() -> None:
    client, _ = await _client(_config(NoneAuth()))
    async with client:
        resp = await client.get("/v1/namespaces")
        assert resp.status_code == 200


@pytest.mark.contract
async def test_info_endpoint_does_not_require_auth() -> None:
    client, _ = await _client(_config(BearerAuth(mode="bearer", tokens=["ok"])))
    async with client:
        resp = await client.get("/info")
        assert resp.status_code == 200
