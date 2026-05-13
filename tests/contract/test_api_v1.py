"""Contract tests for the i3X v1 surface, driven by the ported Python client."""

from __future__ import annotations

import pytest

from i3xua.i3x.client import I3XClient


@pytest.mark.contract
async def test_namespaces_returned(v1_client: I3XClient) -> None:
    namespaces = await v1_client.get_namespaces()
    uris = {n.uri for n in namespaces}
    assert "urn:demo#connection=conn_ref" in uris
    assert "http://opcfoundation.org/UA/#connection=conn_ref" in uris


@pytest.mark.contract
async def test_object_types_filtered_by_namespace(v1_client: I3XClient) -> None:
    types = await v1_client.get_object_types("urn:demo#connection=conn_ref")
    assert [t.sourceTypeId for t in types] == ["BoilerType"]
    assert types[0].schema_["properties"]["Temp"]["type"] == "number"


@pytest.mark.contract
async def test_object_types_query_bulk(v1_client: I3XClient) -> None:
    # Client doesn't wrap this method; hit the route directly to exercise the bulk shape.
    # ObjectType elementIds use the type's `display_name` as the NodeId-segment
    # (D-69 wire convention): `conn_ref!BoilerType`, not `conn_ref!ns=2;s=BoilerType`.
    resp = await v1_client._http.post(  # type: ignore[attr-defined]
        f"{v1_client.base_url}/objecttypes/query",
        headers={"Authorization": "Bearer test-token"},
        json={"elementIds": ["conn_ref!BoilerType", "conn_ref!Missing"]},
    )
    body = resp.json()
    assert body["success"] is False  # one of the items failed
    by_id = {r["elementId"]: r for r in body["results"]}
    assert by_id["conn_ref!BoilerType"]["success"] is True
    assert by_id["conn_ref!Missing"]["success"] is False


@pytest.mark.contract
async def test_objects_includes_all_seeded_instances(v1_client: I3XClient) -> None:
    instances = await v1_client.get_objects(include_metadata=True)
    ids = {i.elementId for i in instances}
    assert ids == {"conn_ref!ns=2;s=Boiler1", "conn_ref!ns=2;s=Boiler1/Temp"}


@pytest.mark.contract
async def test_objects_root_filter(v1_client: I3XClient) -> None:
    roots = await v1_client.get_objects(root=True)
    assert [r.elementId for r in roots] == ["conn_ref!ns=2;s=Boiler1"]


@pytest.mark.contract
async def test_related_returns_composition_children(v1_client: I3XClient) -> None:
    children = await v1_client.get_related_objects(
        "conn_ref!ns=2;s=Boiler1", relationship_type="HasComponent"
    )
    assert [c.elementId for c in children] == ["conn_ref!ns=2;s=Boiler1/Temp"]


@pytest.mark.contract
async def test_read_value_returns_part6_encoded_body(v1_client: I3XClient) -> None:
    lkv = await v1_client.get_value("conn_ref!ns=2;s=Boiler1/Temp")
    assert lkv is not None
    assert lkv.value == 88.4
    assert lkv.quality == "Good"


@pytest.mark.contract
async def test_write_is_rejected_with_405(v1_client: I3XClient) -> None:
    resp = await v1_client._http.put(  # type: ignore[attr-defined]
        f"{v1_client.base_url}/objects/conn_ref!ns=2;s=Boiler1/Temp/value",
        headers={"Authorization": "Bearer test-token"},
        json={"value": 1},
    )
    assert resp.status_code == 405


@pytest.mark.contract
async def test_subscription_lifecycle_and_sync(v1_client: I3XClient) -> None:
    created = await v1_client.create_subscription()
    sid = created.subscriptionId

    await v1_client.register_monitored_items(sid, ["conn_ref!ns=2;s=Boiler1/Temp"])

    # Simulate adapter fanning out a sample to the subscription ring.
    import httpx

    from i3xua.core.neutral import Quality, ValueSample

    # Reach into app state via the ASGI transport the client is using.
    transport = v1_client._http._transport  # type: ignore[attr-defined]
    app_state = transport.app.state.app_state  # type: ignore[attr-defined]
    _ = httpx
    sample = ValueSample(
        element_id="conn_ref!ns=2;s=Boiler1/Temp",
        value={"Type": 11, "Body": 90.0},
        quality=Quality.Good,
        timestamp="2026-04-14T01:30:00Z",
    )
    await app_state.subscriptions.push(sample)

    items = await v1_client.sync(sid)
    assert len(items) == 1
    assert items[0].elementId == "conn_ref!ns=2;s=Boiler1/Temp"
    assert items[0].sequenceNumber == 1

    # A second sync with the last seq should return nothing new.
    items_empty = await v1_client.sync(sid)
    assert items_empty == []

    await v1_client.unregister_monitored_items(sid, ["conn_ref!ns=2;s=Boiler1/Temp"])
    await v1_client.delete_subscription(sid)


@pytest.mark.contract
async def test_history_returns_seeded_samples(v1_client: I3XClient) -> None:
    history = await v1_client.get_history("conn_ref!ns=2;s=Boiler1/Temp")
    # v1 returns HistoricalValueResult with .values: list[VQT].
    assert len(history.values) == 1
    assert history.values[0].quality == "Good"
