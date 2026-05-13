"""Value-read semantics by node class — composition walking + Object filter.

CESMII RFC §3.2.3: a composition Object's value is composed of its
HasComponent children's values, surfaced as `components: {name: VQT}`. Plain
(non-composition) Objects get the empty-Bad placeholder with
`quality="GoodNoData"`. Neither category triggers an OPC UA Read on the
Object itself (which would yield `BadAttributeIdInvalid`).
"""

from __future__ import annotations

import httpx
import pytest

from i3xua.api.state import AppState
from i3xua.core.neutral import NodeClass, NodeDescriptor


@pytest.mark.contract
async def test_reading_composition_object_returns_components(
    http_client: httpx.AsyncClient, app_state: AppState
) -> None:
    """Boiler1 is `is_composition=True` with child `Temp` (Variable). The
    value request for Boiler1 must walk HasComponent, batch-read Temp, and
    surface it under `components: {Temp: VQT}` — NOT the empty-Bad
    placeholder and NOT a failed Object Read on the wire."""
    object_element_id = "conn_ref!ns=2;s=Boiler1"

    hits: list[list[str]] = []
    original = app_state.upstream.read_values

    async def tracking_read(conn, node_ids):  # type: ignore[no-untyped-def]
        hits.append(list(node_ids))
        return await original(conn, node_ids)

    app_state.upstream.read_values = tracking_read  # type: ignore[assignment]
    try:
        resp = await http_client.post(
            "/v1/objects/value",
            headers={"Authorization": "Bearer test-token"},
            json={"elementIds": [object_element_id]},
        )
        body = resp.json()
    finally:
        app_state.upstream.read_values = original  # type: ignore[assignment]

    assert resp.status_code == 200
    entry = body["results"][0]
    assert entry["success"] is True
    assert entry["elementId"] == object_element_id
    # Composition response: value is null, components keyed by elementId.
    result = entry["result"]
    assert result["value"] is None
    assert result["isComposition"] is True
    comp = result.get("components", {})
    temp_key = "conn_ref!ns=2;s=Boiler1/Temp"
    assert temp_key in comp
    assert comp[temp_key]["quality"] == "Good"
    # Wire: the Object itself is NEVER read; only its leaf Variable is.
    assert hits == [["ns=2;s=Boiler1/Temp"]]


@pytest.mark.contract
async def test_reading_variable_still_hits_upstream(
    http_client: httpx.AsyncClient, app_state: AppState
) -> None:
    variable_element_id = "conn_ref!ns=2;s=Boiler1/Temp"
    hits: list[list[str]] = []
    original = app_state.upstream.read_values

    async def tracking_read(conn, node_ids):  # type: ignore[no-untyped-def]
        hits.append(list(node_ids))
        return await original(conn, node_ids)

    app_state.upstream.read_values = tracking_read  # type: ignore[assignment]
    try:
        resp = await http_client.post(
            "/v1/objects/value",
            headers={"Authorization": "Bearer test-token"},
            json={"elementIds": [variable_element_id]},
        )
    finally:
        app_state.upstream.read_values = original  # type: ignore[assignment]

    assert resp.status_code == 200
    assert hits == [["ns=2;s=Boiler1/Temp"]]


@pytest.mark.contract
async def test_mixed_batch_batches_leaf_reads(
    http_client: httpx.AsyncClient, app_state: AppState
) -> None:
    """Composition Object + its leaf Variable in one batch ⇒ one wire read
    for the shared leaf; Object response carries `components`, Variable
    response carries the direct VQT."""
    object_id = "conn_ref!ns=2;s=Boiler1"
    variable_id = "conn_ref!ns=2;s=Boiler1/Temp"
    hits: list[list[str]] = []
    original = app_state.upstream.read_values

    async def tracking_read(conn, node_ids):  # type: ignore[no-untyped-def]
        hits.append(list(node_ids))
        return await original(conn, node_ids)

    app_state.upstream.read_values = tracking_read  # type: ignore[assignment]
    try:
        resp = await http_client.post(
            "/v1/objects/value",
            headers={"Authorization": "Bearer test-token"},
            json={"elementIds": [object_id, variable_id]},
        )
        body = resp.json()
    finally:
        app_state.upstream.read_values = original  # type: ignore[assignment]

    assert resp.status_code == 200
    assert hits == [["ns=2;s=Boiler1/Temp"]]
    by_id = {r["elementId"]: r for r in body["results"]}
    assert by_id[variable_id]["result"]["quality"] == "Good"
    assert by_id[object_id]["result"].get("components") is not None
    assert by_id[object_id]["result"]["quality"] == "Good"


@pytest.mark.contract
async def test_non_composition_object_returns_empty_placeholder(
    http_client: httpx.AsyncClient, app_state: AppState
) -> None:
    """A folder-like Object with no HasComponent children gets the empty-Bad
    placeholder and does NOT touch the wire."""
    folder_id = "conn_ref!ns=2;s=Folder1"

    # Add a non-composition Object to the instance registry for this test.
    nodes = list(app_state.instances.snapshot().values())
    nodes.append(
        NodeDescriptor(
            node_id="ns=2;s=Folder1",
            connection="conn_ref",
            display_name="Folder1",
            node_class=NodeClass.Object,
            namespace_uri="urn:demo",
            type_source_id=None,
            parent_node_id=None,
            is_composition=False,
        )
    )
    await app_state.instances.reconcile(nodes)

    hits: list[list[str]] = []
    original = app_state.upstream.read_values

    async def tracking_read(conn, node_ids):  # type: ignore[no-untyped-def]
        hits.append(list(node_ids))
        return await original(conn, node_ids)

    app_state.upstream.read_values = tracking_read  # type: ignore[assignment]
    try:
        resp = await http_client.post(
            "/v1/objects/value",
            headers={"Authorization": "Bearer test-token"},
            json={"elementIds": [folder_id]},
        )
        body = resp.json()
    finally:
        app_state.upstream.read_values = original  # type: ignore[assignment]

    assert resp.status_code == 200
    result = body["results"][0]["result"]
    assert result["value"] == {}
    assert result["quality"] == "GoodNoData"
    assert "components" not in result  # absent, not null
    # Crucially: zero wire traffic on folder-like Objects.
    assert hits == []
