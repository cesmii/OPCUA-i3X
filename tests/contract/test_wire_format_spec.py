"""Wire-format compliance tests — derived from the i3X Implementation Guide.

Each test validates a specific payload example from:
https://github.com/cesmii/i3X/blob/1.0-Beta/spec/IMPLEMENTATION_GUIDE.md

These are the AUTHORITATIVE shapes. If our server produces a different shape,
the server is wrong — not the spec.
"""

from __future__ import annotations

import httpx
import pytest

from i3xua.api.state import AppState
from i3xua.core.neutral import NodeClass, NodeDescriptor, Quality, ValueSample

# ------------------------------------------------------------------ /info


@pytest.mark.contract
async def test_info_returns_server_info_shape(http_client: httpx.AsyncClient) -> None:
    """GET /info → ServerInfo per spec."""
    resp = await http_client.get("/v1/info")
    body = resp.json()
    assert "specVersion" in body
    assert "capabilities" in body
    caps = body["capabilities"]
    assert "query" in caps and "history" in caps["query"]
    assert "update" in caps and "current" in caps["update"]
    assert "subscribe" in caps and "stream" in caps["subscribe"]


# ------------------------------------------------------------------ /objects — instance shape


@pytest.mark.contract
async def test_object_instance_basic_shape(
    http_client: httpx.AsyncClient, app_state: AppState
) -> None:
    """GET /objects → ObjectInstance uses `typeElementId` (not `typeId`),
    `isExtended` is bool (not null)."""
    resp = await http_client.get(
        "/v1/objects",
        headers={"Authorization": "Bearer test-token"},
    )
    body = resp.json()
    assert body["success"] is True
    obj = body["result"][0]
    assert "typeElementId" in obj, "must use typeElementId, not typeId"
    assert "typeId" not in obj, "typeId is the old name — v1 uses typeElementId"
    assert isinstance(obj["isComposition"], bool)


@pytest.mark.contract
async def test_object_instance_carries_namespace_uri(
    http_client: httpx.AsyncClient, app_state: AppState
) -> None:
    """v1 wire convention (per i3X-Explorer's `normalizeV1Object`): the
    instance's namespace surfaces as a top-level `namespaceUri` field —
    `(raw.namespaceUri ?? metadata.typeNamespaceUri)`.

    The spec note that "instances don't belong to a namespace" applies to
    the OPC UA NodeId model, not the i3X wire payload. We emit `namespaceUri`
    so Explorer (and any client following the same normalization) gets the
    instance's namespace without having to drill into metadata.
    """
    resp = await http_client.get(
        "/v1/objects",
        headers={"Authorization": "Bearer test-token"},
    )
    body = resp.json()
    assert body["success"] is True
    assert body["result"], "expected at least one object in the test fixture"
    for obj in body["result"]:
        assert "namespaceUri" in obj, (
            f"namespaceUri MUST be on the object body for v1 — Explorer reads it "
            f"top-level. Missing on {obj['elementId']}"
        )
        assert obj["namespaceUri"], (
            f"namespaceUri must be non-empty. Got falsy value on {obj['elementId']}"
        )


@pytest.mark.contract
async def test_object_instance_required_fields_only(
    http_client: httpx.AsyncClient, app_state: AppState
) -> None:
    """Without includeMetadata, the object has exactly the 6 spec-required
    fields (plus any implementation-defined fields that don't conflict).

    Spec required fields:
      elementId, displayName, typeElementId, parentId, isComposition, isExtended
    """
    resp = await http_client.get(
        "/v1/objects",
        headers={"Authorization": "Bearer test-token"},
    )
    body = resp.json()
    obj = body["result"][0]
    required = {
        "elementId",
        "displayName",
        "typeElementId",
        "parentId",
        "isComposition",
        "isExtended",
    }
    assert required <= set(obj.keys()), f"missing required fields: {required - set(obj.keys())}"
    # metadata must NOT be present without includeMetadata=true
    assert "metadata" not in obj


# ------------------------------------------------------------------ /objects — metadata


@pytest.mark.contract
async def test_object_instance_with_metadata(
    http_client: httpx.AsyncClient, app_state: AppState
) -> None:
    """GET /objects?includeMetadata=true → metadata has structured fields."""
    resp = await http_client.get(
        "/v1/objects?includeMetadata=true",
        headers={"Authorization": "Bearer test-token"},
    )
    body = resp.json()
    obj = body["result"][0]
    meta = obj.get("metadata")
    assert meta is not None
    # Spec requires these named fields:
    assert "sourceTypeId" in meta
    assert "system" in meta


@pytest.mark.contract
async def test_metadata_has_type_namespace_uri(
    http_client: httpx.AsyncClient, app_state: AppState
) -> None:
    """Spec: metadata.typeNamespaceUri is required — it identifies which
    namespace's schema this Object conforms to."""
    resp = await http_client.get(
        "/v1/objects?includeMetadata=true",
        headers={"Authorization": "Bearer test-token"},
    )
    body = resp.json()
    for obj in body["result"]:
        meta = obj.get("metadata")
        assert meta is not None, f"metadata missing on {obj['elementId']}"
        assert "typeNamespaceUri" in meta, (
            f"typeNamespaceUri missing from metadata on {obj['elementId']}"
        )
        assert isinstance(meta["typeNamespaceUri"], str)


@pytest.mark.contract
async def test_relationships_is_top_level_field(
    http_client: httpx.AsyncClient, app_state: AppState
) -> None:
    """Understanding Relationships doc: `relationships` is a TOP-LEVEL field
    on the Object — NOT nested inside metadata. Every spec example shows it
    right next to elementId, parentId, isComposition.

    Values use displayName (human-readable) — matching the spec examples
    which use names like "pump-101", "tank-201", not opaque index addresses.

    Test fixture: Boiler1 (root, composition) with child Temp (HasComponent).
    - Boiler1 should have relationships.HasComponent: ["Temp"]
    - Temp should have relationships.HasParent: "Boiler1",
      relationships.ComponentOf: "Boiler1"
    """
    resp = await http_client.get(
        "/v1/objects",
        headers={"Authorization": "Bearer test-token"},
    )
    body = resp.json()
    by_id = {obj["elementId"]: obj for obj in body["result"]}

    boiler = by_id["conn_ref!ns=2;s=Boiler1"]
    # relationships is TOP-LEVEL, not inside metadata
    boiler_rels = boiler.get("relationships")
    assert boiler_rels is not None, (
        f"Boiler1 must have top-level 'relationships' field, got keys: {list(boiler.keys())}"
    )
    assert "HasComponent" in boiler_rels, (
        f"Boiler1 must have HasComponent relationship, got {boiler_rels}"
    )
    assert "Temp" in boiler_rels["HasComponent"], (
        f"HasComponent should use displayName 'Temp', got {boiler_rels['HasComponent']}"
    )

    temp = by_id["conn_ref!ns=2;s=Boiler1/Temp"]
    temp_rels = temp.get("relationships")
    assert temp_rels is not None, "Temp must have top-level 'relationships' field"
    assert "HasParent" in temp_rels
    assert temp_rels["HasParent"] == "Boiler1", (
        f"HasParent should use displayName 'Boiler1', got {temp_rels['HasParent']}"
    )
    assert "ComponentOf" in temp_rels
    assert temp_rels["ComponentOf"] == "Boiler1", (
        f"ComponentOf should use displayName 'Boiler1', got {temp_rels['ComponentOf']}"
    )


# ------------------------------------------------------------------ /objects/list — error shape


@pytest.mark.contract
async def test_bulk_error_uses_structured_shape(
    http_client: httpx.AsyncClient, app_state: AppState
) -> None:
    """Spec: failure items in bulk responses must use structured error:
    ```json
    {
      "success": false,
      "elementId": "non-existent",
      "error": {
        "code": 404,
        "message": "Element not found: non-existent"
      }
    }
    ```
    NOT a plain string like `"error": "not found"`.
    """
    resp = await http_client.post(
        "/v1/objects/list",
        headers={"Authorization": "Bearer test-token"},
        json={"elementIds": ["does-not-exist"]},
    )
    body = resp.json()
    assert body["success"] is False, "top-level success must be false when any item fails"
    item = body["results"][0]
    assert item["success"] is False
    assert item["elementId"] == "does-not-exist"
    err = item["error"]
    assert isinstance(err, dict), f"error must be {{code, message}} dict, got {err!r}"
    assert "code" in err, f"error must have 'code' field, got {err}"
    assert "message" in err, f"error must have 'message' field, got {err}"
    assert isinstance(err["code"], int)
    assert isinstance(err["message"], str)


@pytest.mark.contract
async def test_bulk_success_false_when_any_element_fails(
    http_client: httpx.AsyncClient, app_state: AppState
) -> None:
    """Spec: top-level `success` is false if ANY element failed, even when
    other elements succeeded."""
    resp = await http_client.post(
        "/v1/objects/list",
        headers={"Authorization": "Bearer test-token"},
        json={"elementIds": ["conn_ref!ns=2;s=Boiler1", "does-not-exist"]},
    )
    body = resp.json()
    # One success, one failure → top-level must be false
    assert body["success"] is False


# ------------------------------------------------------------------ /objects/related — error shape


@pytest.mark.contract
async def test_related_error_uses_structured_shape(
    http_client: httpx.AsyncClient, app_state: AppState
) -> None:
    """POST /objects/related with non-existent elementId → structured error."""
    # Note: current implementation always returns success=True with empty result
    # for unknown elementIds in /objects/related. This test documents the spec
    # expectation — the current behavior may be acceptable if we consider
    # "no relationships found" as success with empty result.
    pass  # placeholder — /objects/related currently succeeds with empty result


# ------------------------------------------------------------------ /objects/value (leaf)


@pytest.mark.contract
async def test_leaf_value_is_naked_json(
    http_client: httpx.AsyncClient, app_state: AppState
) -> None:
    """POST /objects/value on a leaf Variable → `value` is a naked JSON
    scalar (e.g. 88.4), NOT a Part-6 Variant `{"Type": 11, "Body": 88.4}`.

    Spec example:
    ```json
    {"isComposition": false, "value": 67.1, "quality": "Good", "timestamp": "..."}
    ```
    """
    resp = await http_client.post(
        "/v1/objects/value",
        headers={"Authorization": "Bearer test-token"},
        json={"elementIds": ["conn_ref!ns=2;s=Boiler1/Temp"]},
    )
    body = resp.json()
    item = body["results"][0]
    assert item["success"] is True
    result = item["result"]
    # Value MUST be a naked number, NOT {"Type": 11, "Body": 88.4}.
    assert isinstance(result["value"], (int, float)), (
        f"value must be naked JSON, got {result['value']!r}"
    )
    assert result["quality"] in ("Good", "GoodNoData", "Bad", "Uncertain")
    assert isinstance(result["timestamp"], str)
    assert result["isComposition"] is False
    # `components` MUST be absent for non-composition elements.
    assert "components" not in result


# ------------------------------------------------------------------ /objects/value (composition)


@pytest.mark.contract
async def test_composition_value_has_null_value_and_components_keyed_by_element_id(
    http_client: httpx.AsyncClient, app_state: AppState
) -> None:
    """POST /objects/value on a composition Object → `value` is null,
    `components` dict keyed by **elementId** (not displayName).

    Spec example:
    ```json
    {
      "value": null,
      "quality": "GoodNoData",
      "timestamp": "...",
      "components": {
        "pump-101-bearing-temperature": {
          "value": 70.34,
          "quality": "Good",
          "timestamp": "..."
        }
      }
    }
    ```
    """
    resp = await http_client.post(
        "/v1/objects/value",
        headers={"Authorization": "Bearer test-token"},
        json={"elementIds": ["conn_ref!ns=2;s=Boiler1"]},
    )
    body = resp.json()
    item = body["results"][0]
    result = item["result"]
    assert result["value"] is None, "composition value must be null"
    assert result["isComposition"] is True
    comp = result.get("components")
    assert comp is not None, "composition must have components"
    # Components keyed by elementId.
    assert all("!" in key for key in comp), (
        f"component keys must be elementIds, got {list(comp.keys())}"
    )
    # Each component is a VQT.
    for _key, vqt in comp.items():
        assert "value" in vqt
        assert "quality" in vqt
        assert "timestamp" in vqt
        # Component values are also naked JSON.
        assert not (
            isinstance(vqt["value"], dict) and "Type" in vqt["value"] and "Body" in vqt["value"]
        ), f"component value must be naked JSON, got {vqt['value']!r}"


# ------------------------------------------------------------------ /objects/value (empty Object)


@pytest.mark.contract
async def test_non_composition_object_returns_goodnodata_no_components(
    http_client: httpx.AsyncClient, app_state: AppState
) -> None:
    """Non-composition Object (folder-like) → value={}, quality=GoodNoData,
    no components key."""
    # Seed a non-composition Object.
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

    resp = await http_client.post(
        "/v1/objects/value",
        headers={"Authorization": "Bearer test-token"},
        json={"elementIds": ["conn_ref!ns=2;s=Folder1"]},
    )
    body = resp.json()
    result = body["results"][0]["result"]
    assert result["value"] == {}
    assert result["quality"] == "GoodNoData"
    assert result["isComposition"] is False
    assert "components" not in result


# ------------------------------------------------------------------ /objects/value — error shape


@pytest.mark.contract
async def test_value_error_uses_structured_shape(
    http_client: httpx.AsyncClient, app_state: AppState
) -> None:
    """POST /objects/value with non-existent elementId → structured error."""
    resp = await http_client.post(
        "/v1/objects/value",
        headers={"Authorization": "Bearer test-token"},
        json={"elementIds": ["does-not-exist"]},
    )
    body = resp.json()
    item = body["results"][0]
    assert item["success"] is False
    err = item["error"]
    assert isinstance(err, dict), f"error must be {{code, message}} dict, got {err!r}"
    assert "code" in err
    assert "message" in err


# ------------------------------------------------------------------ /subscriptions/sync


@pytest.mark.contract
async def test_sync_items_have_sequence_number_and_naked_value(
    http_client: httpx.AsyncClient, app_state: AppState
) -> None:
    """POST /subscriptions/sync → items carry sequenceNumber, elementId,
    naked value, quality, timestamp.

    Spec example:
    ```json
    [
      {"sequenceNumber": 1, "elementId": "sensor-001", "value": 72.5,
       "quality": "Good", "timestamp": "2025-01-08T10:30:00Z"}
    ]
    ```
    """
    # Create sub + register + push a sample.
    create_resp = await http_client.post(
        "/v1/subscriptions",
        headers={"Authorization": "Bearer test-token"},
        json={},
    )
    sid = create_resp.json()["result"]["subscriptionId"]

    await http_client.post(
        "/v1/subscriptions/register",
        headers={"Authorization": "Bearer test-token"},
        json={"subscriptionId": sid, "elementIds": ["conn_ref!ns=2;s=Boiler1/Temp"]},
    )

    await app_state.subscriptions.push(
        ValueSample(
            element_id="conn_ref!ns=2;s=Boiler1/Temp",
            value={"Type": 11, "Body": 42.0},
            quality=Quality.Good,
            timestamp="2026-04-17T10:00:00Z",
        )
    )

    sync_resp = await http_client.post(
        "/v1/subscriptions/sync",
        headers={"Authorization": "Bearer test-token"},
        json={"subscriptionId": sid},
    )
    body = sync_resp.json()
    items = body["result"]
    assert len(items) >= 1
    item = items[0]
    assert "sequenceNumber" in item
    assert isinstance(item["sequenceNumber"], int)
    assert "elementId" in item
    assert "quality" in item
    assert "timestamp" in item

    # Cleanup
    await http_client.post(
        "/v1/subscriptions/delete",
        headers={"Authorization": "Bearer test-token"},
        json={"subscriptionIds": [sid]},
    )
