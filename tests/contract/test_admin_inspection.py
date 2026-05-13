"""Read-only /admin/config + /admin/state inspection endpoints.

Backs the `/admin/ui` read-only explorer. No writes; no runtime mutation.
"""

from __future__ import annotations

import httpx
import pytest

from i3xua.api.state import AppState
from i3xua.core.neutral import Quality, ValueSample

# ------------------------------------------------------------------ /admin/config


@pytest.mark.contract
async def test_admin_config_requires_auth(http_client: httpx.AsyncClient) -> None:
    """/admin/config must sit behind the same auth as the rest of /admin."""
    resp = await http_client.get("/v1/admin/config")
    assert resp.status_code == 401


@pytest.mark.contract
async def test_admin_config_returns_parsed_shape(
    http_client: httpx.AsyncClient, app_state: AppState
) -> None:
    """GET /admin/config returns the parsed AppConfig as JSON. Top-level
    sections should mirror the YAML schema: server, logging, registry,
    threads, connections."""
    resp = await http_client.get(
        "/v1/admin/config",
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "server" in body
    assert "logging" in body
    assert "registry" in body
    assert "connections" in body
    # At least one connection from the fixture.
    assert len(body["connections"]) >= 1
    conn = body["connections"][0]
    assert conn["name"] == "conn_ref"
    assert "endpoint" in conn


@pytest.mark.contract
async def test_admin_config_redacts_bearer_tokens(
    http_client: httpx.AsyncClient, app_state: AppState
) -> None:
    """Bearer tokens must NOT leak in the config dump — redact to '***'."""
    resp = await http_client.get(
        "/v1/admin/config",
        headers={"Authorization": "Bearer test-token"},
    )
    body = resp.json()
    auth = body["server"]["auth"]
    if auth["mode"] == "bearer":
        for tok in auth["tokens"]:
            assert tok == "***", f"bearer token must be redacted, got {tok!r}"


# ------------------------------------------------------------------ /admin/state


@pytest.mark.contract
async def test_admin_state_requires_auth(http_client: httpx.AsyncClient) -> None:
    resp = await http_client.get("/v1/admin/state")
    assert resp.status_code == 401


@pytest.mark.contract
async def test_admin_state_reports_registry_counts(
    http_client: httpx.AsyncClient, app_state: AppState
) -> None:
    """Fixture seeds 2 namespaces, 1 type, 2 instances (Boiler1 + Temp)."""
    resp = await http_client.get(
        "/v1/admin/state",
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["namespaces"]["count"] == 2
    assert body["types"]["count"] == 1
    assert body["instances"]["count"] == 2
    # Namespaces list surfaces the URIs so an operator can see what was
    # discovered at a glance.
    uris = body["namespaces"]["uris"]
    assert isinstance(uris, list)
    assert any("urn:demo" in u for u in uris)


@pytest.mark.contract
async def test_admin_state_reports_connections(
    http_client: httpx.AsyncClient, app_state: AppState
) -> None:
    """State lists every configured connection by name + endpoint so the
    operator knows what the wrapper is *trying* to talk to. Live status
    (connected / reconnecting) is a future Port extension."""
    resp = await http_client.get(
        "/v1/admin/state",
        headers={"Authorization": "Bearer test-token"},
    )
    body = resp.json()
    conns = body["connections"]
    assert isinstance(conns, list)
    assert len(conns) == 1
    assert conns[0]["name"] == "conn_ref"
    assert conns[0]["endpoint"].startswith("opc.tcp://")


@pytest.mark.contract
async def test_admin_state_reports_subscriptions(
    http_client: httpx.AsyncClient, app_state: AppState
) -> None:
    """After creating an i3X subscription and registering one element,
    /admin/state should report one subscription with element_count=1."""
    create = await http_client.post(
        "/v1/subscriptions",
        headers={"Authorization": "Bearer test-token"},
        json={},
    )
    sid = create.json()["result"]["subscriptionId"]
    await http_client.post(
        "/v1/subscriptions/register",
        headers={"Authorization": "Bearer test-token"},
        json={"subscriptionId": sid, "elementIds": ["conn_ref!ns=2;s=Boiler1/Temp"]},
    )

    resp = await http_client.get(
        "/v1/admin/state",
        headers={"Authorization": "Bearer test-token"},
    )
    body = resp.json()
    subs = body["subscriptions"]
    assert isinstance(subs, list)
    found = next((s for s in subs if s["id"] == sid), None)
    assert found is not None, f"subscription {sid} missing from state"
    assert found["element_count"] == 1


@pytest.mark.contract
async def test_admin_ui_served_without_auth(
    http_client: httpx.AsyncClient,
) -> None:
    """The HTML shell itself has no secrets — the JS inside prompts for a
    bearer token that the browser then uses for /admin/config + /admin/state.
    The page must load without auth so users can paste their token in."""
    resp = await http_client.get("/v1/admin/ui")
    assert resp.status_code == 200
    body = resp.text
    # Sanity: it's actually HTML with the pieces the page needs.
    assert "<title>i3xua" in body
    assert "/v1/admin/config" in body
    assert "/v1/admin/state" in body
    # Token input exists.
    assert 'id="tok"' in body


@pytest.mark.contract
async def test_admin_state_reports_history_depth(
    http_client: httpx.AsyncClient, app_state: AppState
) -> None:
    """Fixture seeded one historical sample for Temp; /admin/state should
    reflect the ring depth per element so operators can see what's buffered."""
    # Add a second sample so the ring has 2 entries.
    await app_state.history.append(
        ValueSample(
            element_id="conn_ref!ns=2;s=Boiler1/Temp",
            value=None,
            quality=Quality.Good,
            timestamp="2026-04-20T00:00:00Z",
        )
    )
    resp = await http_client.get(
        "/v1/admin/state",
        headers={"Authorization": "Bearer test-token"},
    )
    body = resp.json()
    hist = body["history"]
    assert hist["elements_tracked"] >= 1
    # The sample fixture puts one entry under Temp; we added one → depth ≥2.
    by_el = {entry["elementId"]: entry["depth"] for entry in hist["elements"]}
    assert by_el["conn_ref!ns=2;s=Boiler1/Temp"] >= 2
