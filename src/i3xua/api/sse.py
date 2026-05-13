"""Server-Sent Events generator shared between v0 and v1 stream routes.

Format: each message is `data: <json-array>\\n\\n`. A heartbeat comment line
(`: heartbeat\\n\\n`) is emitted at `heartbeat_seconds` intervals so idle
connections survive proxies that kill silent TCP streams.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import Request

from i3xua.api.state import AppState


async def sse_stream(
    request: Request,
    state: AppState,
    subscription_id: str,
    *,
    heartbeat_seconds: float = 30.0,
) -> AsyncIterator[str]:
    # Starlette's StreamingResponse cancels the generator on client disconnect,
    # so polling `request.is_disconnected()` is unnecessary.
    _ = request
    queue = await state.subscriptions.attach_stream(subscription_id)
    try:
        yield ": open\n\n"
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=heartbeat_seconds)
            except TimeoutError:
                yield ": heartbeat\n\n"
                continue
            if item is None:
                return
            payload: list[dict[str, Any]] = [item.model_dump(by_alias=True)]
            yield f"data: {json.dumps(payload)}\n\n"
    finally:
        await state.subscriptions.detach_stream(subscription_id, queue)


__all__ = ["sse_stream"]
