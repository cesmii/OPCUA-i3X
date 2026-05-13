"""Live tests against the OPC Foundation Quickstart Reference Server.

Gated with `@pytest.mark.live` so CI (which doesn't have the server) skips
them. Run locally with:

    uv run pytest -m live

Prerequisites:
 - The Reference Server is listening at `OPCUA_TEST_ENDPOINT`
   (default `opc.tcp://mac:62541/UA/Quickstarts/ReferenceServer`).
 - The wrapper uses that URL literally; the `/UA` prefix is necessary to make
   asyncua's synthesized `ServerUri` match the server's ApplicationUri.
"""

from __future__ import annotations

import asyncio
import os
import socket
import threading
from collections.abc import AsyncIterator

import httpx
import pytest
import uvicorn

from i3xua.adapters.asyncua.upstream import AsyncuaUpstreamDataSource
from i3xua.api.app_factory import build_app
from i3xua.api.state import build_state
from i3xua.i3x.client import BearerCredentials, I3XClient
from i3xua.settings import (
    AppConfig,
    BearerAuth,
    ConnectionConfig,
    ServerConfig,
)

DEFAULT_ENDPOINT = "opc.tcp://mac:62541/Quickstarts/ReferenceServer"
TOKEN = "live-token"


def _config() -> AppConfig:
    endpoint = os.environ.get("OPCUA_TEST_ENDPOINT", DEFAULT_ENDPOINT)
    return AppConfig(
        server=ServerConfig(
            host="127.0.0.1",
            port=8081,
            versions=["v0", "v1"],
            auth=BearerAuth(mode="bearer", tokens=[TOKEN]),
        ),
        connections=[
            ConnectionConfig(
                name="conn_ref",
                endpoint=endpoint,
                default_publishing_interval_ms=500,
                default_sampling_interval_ms=500,
            )
        ],
    )


@pytest.fixture
async def live_stack() -> AsyncIterator[tuple[str, object]]:
    """Build the full wrapper stack and serve it on loopback.

    Yields `(base_url, app_state)` so tests can interact via the Python i3X
    client and occasionally reach into app_state for diagnostics.
    """
    cfg = _config()
    upstream = AsyncuaUpstreamDataSource(cfg)
    state = build_state(cfg, upstream=upstream)
    upstream.bind_registries(
        namespaces=state.namespaces,
        types=state.types,
        instances=state.instances,
        subscriptions=state.subscriptions,
        history=state.history,
    )
    await upstream.start()
    # Give the initial browse a chance to populate registries.
    for _ in range(50):
        if state.namespaces.snapshot():
            break
        await asyncio.sleep(0.2)

    app = build_app(state)

    # Real uvicorn on loopback so SSE/TCP tests are faithful.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    server_cfg = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"
    )
    server = uvicorn.Server(server_cfg)

    thread = threading.Thread(target=server.run, name=f"uvicorn-{port}", daemon=True)
    thread.start()

    for _ in range(500):
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await writer.wait_closed()
            _ = reader
            break
        except OSError:
            await asyncio.sleep(0.02)
    else:
        raise RuntimeError(f"uvicorn did not start on port {port}")

    try:
        yield f"http://127.0.0.1:{port}", state
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)
        await upstream.stop()


@pytest.mark.live
async def test_namespaces_include_server_and_boilers(
    live_stack: tuple[str, object],
) -> None:
    base_url, _state = live_stack
    async with (
        httpx.AsyncClient() as http,
        I3XClient(f"{base_url}/v1", BearerCredentials(token=TOKEN), http=http) as client,
    ):
        await client.detect_version()
        namespaces = await client.get_namespaces()
        uris = {n.uri for n in namespaces}
        # Every exposed namespace is suffixed with #connection=conn_ref.
        assert any("opcfoundation.org/UA/Boiler" in u for u in uris), uris
        assert any("mac:UA:Quickstarts:ReferenceServer" in u for u in uris), uris


@pytest.mark.live
async def test_objects_folder_children_are_discovered(
    live_stack: tuple[str, object],
) -> None:
    base_url, state = live_stack
    # Diagnostics: what's in the registries after the initial browse?
    ns_snap = state.namespaces.snapshot()  # type: ignore[attr-defined]
    inst_snap = state.instances.snapshot()  # type: ignore[attr-defined]
    print(
        f"DIAG: namespaces={len(ns_snap)}, instances={len(inst_snap)}, "
        f"sample_instances={list(inst_snap.keys())[:5]}"
    )
    async with (
        httpx.AsyncClient() as http,
        I3XClient(f"{base_url}/v1", BearerCredentials(token=TOKEN), http=http) as client,
    ):
        await client.detect_version()
        objects = await client.get_objects()
        assert objects, (
            f"expected at least one i3X object after live browse "
            f"(registry had {len(inst_snap)} instances)"
        )
        assert all(o.elementId.startswith("conn_ref!") for o in objects)
        display_names = {o.displayName for o in objects}
        assert any("Boiler" in n or "Server" in n for n in display_names), display_names


@pytest.mark.live
async def test_subscription_creates_real_monitored_item_and_delivers_sample(
    live_stack: tuple[str, object],
) -> None:
    """acceptance: registering for a known dynamic variable MUST result
    in a server-side MonitoredItem and the wrapper MUST receive a sample."""
    base_url, state = live_stack
    # Server.ServerStatus.CurrentTime updates every second. We register using
    # the namespace-prefixed form; the server canonicalizes to asyncua's short
    # form (`i=2258`, no `ns=0;`) so that's what the sync response carries.
    registered_id = "conn_ref!ns=0;i=2258"
    canonical_id = "conn_ref!i=2258"

    async with (
        httpx.AsyncClient() as http,
        I3XClient(f"{base_url}/v1", BearerCredentials(token=TOKEN), http=http) as client,
    ):
        await client.detect_version()

        created = await client.create_subscription()
        sid = created.subscriptionId
        await client.register_monitored_items(sid, [registered_id])

        # Poll the i3X sync endpoint until a sample shows up (or we give up).
        samples: list = []
        for _ in range(40):  # ~8 s at 200 ms each
            items = await client.sync(sid)
            if items:
                samples = items
                break
            await asyncio.sleep(0.2)

        assert samples, "no sample received from CurrentTime subscription"
        sample = samples[0]
        assert sample.elementId == canonical_id
        assert sample.sequenceNumber is not None and sample.sequenceNumber >= 1

        await client.unregister_monitored_items(sid, [registered_id])
        await client.delete_subscription(sid)
        _ = state


@pytest.mark.live
async def test_read_current_value_returns_part6_body(
    live_stack: tuple[str, object],
) -> None:
    base_url, _state = live_stack
    async with (
        httpx.AsyncClient() as http,
        I3XClient(f"{base_url}/v1", BearerCredentials(token=TOKEN), http=http) as client,
    ):
        await client.detect_version()
        # Either form works on the read path (canonicalized server-side).
        lkv = await client.get_value("conn_ref!ns=0;i=2258") or await client.get_value(
            "conn_ref!i=2258"
        )
        assert lkv is not None
        # Part-6 Variant: {"Type": <VariantType>, "Body": <...>}
        assert isinstance(lkv.value, dict)
        assert "Type" in lkv.value
        assert "Body" in lkv.value
        assert lkv.quality in {"Good", "GoodNoData"}


@pytest.mark.live
async def test_objecttypes_includes_variabletypes(
    live_stack: tuple[str, object],
) -> None:
    """`/objecttypes` includes both ObjectTypes and VariableTypes after Task 9."""
    base_url, _state = live_stack
    async with (
        httpx.AsyncClient() as http,
        I3XClient(f"{base_url}/v1", BearerCredentials(token=TOKEN), http=http) as client,
    ):
        await client.detect_version()
        types = await client.get_object_types()
        display_names = {t.displayName for t in types}
        element_ids = {t.elementId for t in types}
        # VariableTypes registered via Task 9 must appear.
        assert "BaseDataVariableType" in display_names, (
            f"BaseDataVariableType missing from /objecttypes; got: {sorted(display_names)}"
        )
        assert "AnalogItemType" in display_names, (
            f"AnalogItemType missing from /objecttypes; got: {sorted(display_names)}"
        )
        # ObjectTypes must still be present too.
        assert "BaseObjectType" in display_names, (
            f"BaseObjectType missing from /objecttypes; got: {sorted(display_names)}"
        )
        # AnalogItemType is ns=0;i=2368 — canonical form strips ns=0 prefix.
        assert "conn_ref!i=2368" in element_ids, (
            f"conn_ref!i=2368 not in element_ids; got: {sorted(element_ids)}"
        )


@pytest.mark.live
async def test_analog_variable_carries_double_datatype(
    live_stack: tuple[str, object],
) -> None:
    """Output (FTX001/Output, ns=5;i=1242) is a Double-typed AnalogItemType."""
    base_url, _state = live_stack
    # Output variable is at ns=5;i=1242, typed by AnalogItemType (i=2368).
    output_eid = "conn_ref!ns=5;i=1242"
    async with (
        httpx.AsyncClient() as http,
        I3XClient(f"{base_url}/v1", BearerCredentials(token=TOKEN), http=http) as client,
    ):
        await client.detect_version()
        inst = await client.get_object(output_eid, include_metadata=True)
        # typeElementId must point to AnalogItemType.
        assert inst.typeElementId == "conn_ref!i=2368", (
            f"Expected typeElementId=conn_ref!i=2368, got {inst.typeElementId!r}"
        )
        # metadata must carry DataType info (Task 11).
        assert inst.metadata is not None, "metadata is None — pass includeMetadata=true"
        assert inst.metadata.dataType == "i=11", (
            f"Expected dataType=i=11 (Double), got {inst.metadata.dataType!r}"
        )
        assert inst.metadata.dataTypeName == "Double", (
            f"Expected dataTypeName=Double, got {inst.metadata.dataTypeName!r}"
        )


@pytest.mark.live
async def test_baseobjecttype_canonical_element_id(
    live_stack: tuple[str, object],
) -> None:
    """BaseObjectType emits as `conn_ref!i=58`, not `conn_ref!ns=0;i=58` (Task 10)."""
    base_url, _state = live_stack
    async with (
        httpx.AsyncClient() as http,
        I3XClient(f"{base_url}/v1", BearerCredentials(token=TOKEN), http=http) as client,
    ):
        await client.detect_version()
        types = await client.get_object_types()
        base_obj = next((t for t in types if t.displayName == "BaseObjectType"), None)
        assert base_obj is not None, "BaseObjectType not found in /objecttypes response"
        assert base_obj.elementId == "conn_ref!i=58", (
            f"BaseObjectType elementId should be conn_ref!i=58 (no ns=0; prefix), "
            f"got {base_obj.elementId!r}"
        )
