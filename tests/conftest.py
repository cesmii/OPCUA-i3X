"""Shared test fixtures: FakeUpstreamDataSource, app factory, i3x async client."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
import pytest
from httpx import ASGITransport

from i3xua.api.app_factory import build_app
from i3xua.api.state import AppState, build_state
from i3xua.core.neutral import (
    BrowseResult,
    ConnectionId,
    NamespaceInfo,
    NodeClass,
    NodeDescriptor,
    Quality,
    SubscriptionHandle,
    TypeDescriptor,
    ValueSample,
)
from i3xua.core.registry import type_structural_hash
from i3xua.i3x.client import BearerCredentials, I3XClient
from i3xua.settings import (
    AppConfig,
    BearerAuth,
    ConnectionConfig,
    ServerConfig,
)


@dataclass
class FakeUpstream:
    """Stand-in for the asyncua adapter: serves pre-seeded browse results,
    remembers monitored-item add/remove, and lets tests push samples."""

    browse: dict[str, BrowseResult]
    values: dict[str, ValueSample] = field(default_factory=dict)
    monitored: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    _sub_manager: object | None = None

    async def list_connections(self) -> list[ConnectionId]:
        return [ConnectionId(name) for name in self.browse]

    async def browse_call(self, connection: ConnectionId) -> BrowseResult:
        return self.browse[connection.name]

    async def read_values(
        self, connection: ConnectionId, node_ids: Iterable[str]
    ) -> list[ValueSample]:
        out: list[ValueSample] = []
        for nid in node_ids:
            key = f"{connection.name}!{nid}"
            out.append(
                self.values.get(
                    key,
                    ValueSample(
                        element_id=key,
                        value=None,
                        quality=Quality.GoodNoData,
                        timestamp="",
                    ),
                )
            )
        return out

    async def add_monitored_items(
        self,
        handle: SubscriptionHandle,
        node_ids: Iterable[str],
        *,
        publishing_ms: int | None = None,
        sampling_ms: int | None = None,
    ) -> None:
        _ = (publishing_ms, sampling_ms)
        self.monitored[handle.connection].update(node_ids)

    async def remove_monitored_items(
        self,
        handle: SubscriptionHandle,
        node_ids: Iterable[str],
        *,
        publishing_ms: int | None = None,
    ) -> None:
        _ = publishing_ms
        self.monitored[handle.connection].difference_update(node_ids)

    def stream(self, handle: SubscriptionHandle) -> AsyncIterator[ValueSample]:
        async def _iter() -> AsyncIterator[ValueSample]:
            if False:  # pragma: no cover - placeholder for SSE wiring
                yield ValueSample(
                    element_id="",
                    value=None,
                    quality=Quality.GoodNoData,
                    timestamp="",
                )

        return _iter()


@pytest.fixture
def config() -> AppConfig:
    return AppConfig(
        server=ServerConfig(
            host="127.0.0.1",
            port=8080,
            versions=["v0", "v1"],
            auth=BearerAuth(mode="bearer", tokens=["test-token"]),
        ),
        connections=[
            ConnectionConfig(
                name="conn_ref",
                endpoint="opc.tcp://test-server:4840",
            )
        ],
    )


def _seed_types(registry, descriptors: list[TypeDescriptor]) -> None:
    from pydantic import create_model

    from i3xua.core.registry import RegisteredType

    for d in descriptors:
        registry._by_hash[d.structural_hash] = RegisteredType(
            descriptor=d, model=create_model(f"T_{d.source_node_id}")
        )
        registry._by_source[f"{d.connection}!{d.source_node_id}"] = d.structural_hash


@pytest.fixture
async def app_state(config: AppConfig) -> AppState:
    upstream = FakeUpstream(browse={})
    state = build_state(config, upstream=upstream)  # type: ignore[arg-type]

    # Seed namespaces — URIs include the per-connection collision suffix
    # (D-34), matching what the browse layer emits in production.
    ns = [
        NamespaceInfo(
            uri="http://opcfoundation.org/UA/#connection=conn_ref",
            connection="conn_ref",
            display_name="UA-ns0",
        ),
        NamespaceInfo(
            uri="urn:demo#connection=conn_ref",
            connection="conn_ref",
            display_name="demo-ns1",
        ),
    ]
    await state.namespaces.reconcile(ns)

    # Seed an ObjectType
    type_desc = TypeDescriptor(
        source_node_id="ns=2;s=BoilerType",
        display_name="BoilerType",
        namespace_uri="urn:demo",
        connection="conn_ref",
        structural_hash=type_structural_hash(
            "ns=2;s=BoilerType", [("Temp", "ns=0;i=11", -1, False)]
        ),
        json_schema={"type": "object", "properties": {"Temp": {"type": "number"}}},
    )
    _seed_types(state.types, [type_desc])

    # Seed instances (Object + Variable)
    instances = [
        NodeDescriptor(
            node_id="ns=2;s=Boiler1",
            connection="conn_ref",
            display_name="Boiler1",
            node_class=NodeClass.Object,
            namespace_uri="urn:demo",
            type_source_id="ns=2;s=BoilerType",
            parent_node_id=None,
            is_composition=True,
        ),
        NodeDescriptor(
            node_id="ns=2;s=Boiler1/Temp",
            connection="conn_ref",
            display_name="Temp",
            node_class=NodeClass.Variable,
            namespace_uri="urn:demo",
            type_source_id="ns=0;i=11",
            parent_node_id="ns=2;s=Boiler1",
            is_composition=False,
            parent_relationship="HasComponent",
        ),
    ]
    await state.instances.reconcile(instances)

    # Seed upstream values
    upstream.values["conn_ref!ns=2;s=Boiler1/Temp"] = ValueSample(
        element_id="conn_ref!ns=2;s=Boiler1/Temp",
        value={"Type": 11, "Body": 88.4},
        quality=Quality.Good,
        timestamp="2026-04-14T01:02:03Z",
    )

    # Seed a historical sample for /objects/history tests.
    await state.history.append(
        ValueSample(
            element_id="conn_ref!ns=2;s=Boiler1/Temp",
            value={"Type": 11, "Body": 80.0},
            quality=Quality.Good,
            timestamp="2026-04-13T00:00:00Z",
        )
    )

    return state


@pytest.fixture
async def http_client(app_state: AppState) -> AsyncIterator[httpx.AsyncClient]:
    app = build_app(app_state)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://i3x.test"
    ) as client:
        yield client


@pytest.fixture
async def live_url(app_state: AppState) -> AsyncIterator[str]:
    """Start uvicorn on 127.0.0.1:<ephemeral> in a dedicated thread so SSE + real
    TCP tests run against a real loopback socket (tshark-friendly).

    Running uvicorn in its own thread avoids the two-event-loop interference we
    saw when trying to `serve()` on the test's own loop. `app_state` is shared
    across loops: all registry writes happen through asyncio.Locks that belong
    to the uvicorn loop, so the test loop only ever reads the app_state
    references it was given (subscriptions.push_to / delete are awaited on the
    same loop the server runs on via anyio internals).
    """
    import socket
    import threading

    import uvicorn

    app = build_app(app_state)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)

    def _run() -> None:
        server.run()

    thread = threading.Thread(target=_run, name=f"uvicorn-{port}", daemon=True)
    thread.start()

    async def _wait_listening() -> None:
        for _ in range(500):
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                await writer.wait_closed()
                _ = reader
                return
            except OSError:
                await asyncio.sleep(0.02)
        raise RuntimeError(f"uvicorn did not start on port {port}")

    await _wait_listening()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)


@pytest.fixture
async def v1_client(http_client: httpx.AsyncClient) -> AsyncIterator[I3XClient]:
    async with I3XClient(
        "http://i3x.test/v1",
        BearerCredentials(token="test-token"),
        http=http_client,
    ) as client:
        await client.detect_version()
        yield client


@pytest.fixture
async def v0_client(http_client: httpx.AsyncClient) -> AsyncIterator[I3XClient]:
    async with I3XClient(
        "http://i3x.test/v0",
        BearerCredentials(token="test-token"),
        http=http_client,
    ) as client:
        # Force v0 without probing /info (which would succeed and auto-upgrade us).
        client._api_version = "v0"  # type: ignore[attr-defined]
        yield client


@pytest.fixture
def _unused_datetime() -> datetime:
    return datetime(2026, 4, 14, tzinfo=UTC)


# Ensure asyncio event loop is torn down cleanly between tests.
@pytest.fixture(autouse=True)
def _cancel_pending_tasks(event_loop: asyncio.AbstractEventLoop | None = None) -> None:
    _ = event_loop
