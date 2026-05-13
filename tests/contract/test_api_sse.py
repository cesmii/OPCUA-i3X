"""Contract tests for SSE streaming.

These tests run against a real uvicorn instance on 127.0.0.1 rather than an
in-memory ASGI transport. That matches how i3X-Explorer connects in production
and lets the operator observe packets on loopback:

    sudo tshark -i lo0 -f "tcp port <port>" -Y http
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from i3xua.api.state import AppState
from i3xua.core.neutral import Quality, ValueSample
from i3xua.i3x.client import BearerCredentials, I3XClient


async def _produce_one_sample(state: AppState, subscription_id: str) -> None:
    # Short delay so the SSE consumer has a chance to attach before we push.
    await asyncio.sleep(0.1)
    await state.subscriptions.push_to(
        subscription_id,
        ValueSample(
            element_id="conn_ref!ns=2;s=Boiler1/Temp",
            value={"Type": 11, "Body": 92.5},
            quality=Quality.Good,
            timestamp="2026-04-14T01:45:00Z",
        ),
    )


@pytest.mark.contract
async def test_sse_v1_stream_delivers_a_sample(live_url: str, app_state: AppState) -> None:
    async with (
        httpx.AsyncClient() as http,
        I3XClient(f"{live_url}/v1", BearerCredentials(token="test-token"), http=http) as client,
    ):
        await client.detect_version()
        created = await client.create_subscription()
        sid = created.subscriptionId
        await client.register_monitored_items(sid, ["conn_ref!ns=2;s=Boiler1/Temp"])

        async def consume() -> list:
            async for batch in client.stream(sid):
                return list(batch)
            return []

        producer = asyncio.create_task(_produce_one_sample(app_state, sid))
        batch = await asyncio.wait_for(consume(), timeout=5.0)
        await producer

        assert len(batch) == 1
        assert batch[0].elementId == "conn_ref!ns=2;s=Boiler1/Temp"
        assert batch[0].sequenceNumber == 1

        await client.delete_subscription(sid)


@pytest.mark.contract
async def test_sse_stream_returns_404_for_unknown_subscription(live_url: str) -> None:
    async with httpx.AsyncClient() as http:
        resp = await http.post(
            f"{live_url}/v1/subscriptions/stream",
            headers={"Authorization": "Bearer test-token"},
            json={"subscriptionId": "sub_does_not_exist"},
        )
        assert resp.status_code == 404


@pytest.mark.contract
async def test_sse_stream_closes_when_subscription_deleted(
    live_url: str, app_state: AppState
) -> None:
    async with (
        httpx.AsyncClient() as http,
        I3XClient(f"{live_url}/v1", BearerCredentials(token="test-token"), http=http) as client,
    ):
        await client.detect_version()
        created = await client.create_subscription()
        sid = created.subscriptionId

        async def consume_until_close() -> None:
            async for _batch in client.stream(sid):
                # Iteration must terminate when the server closes the stream.
                pass

        consumer = asyncio.create_task(consume_until_close())
        await asyncio.sleep(0.1)
        await app_state.subscriptions.delete(sid)
        await asyncio.wait_for(consumer, timeout=5.0)
