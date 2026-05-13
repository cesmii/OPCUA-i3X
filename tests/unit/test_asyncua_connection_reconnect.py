"""\u2014 connection lifecycle + backoff.

Uses a FakeUaClient that satisfies the `UaClientLike` Protocol; no real asyncua
code is exercised. Time is frozen via `monkeypatch`ed `asyncio.sleep` to keep
the suite fast but still verify the actual backoff sequence.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from i3xua.adapters.asyncua.connection import (
    AsyncuaConnection,
    UaClientLike,
)
from i3xua.settings import (
    AnonymousUser,
    ChannelNone,
    ConnectionConfig,
    ReconnectConfig,
)


@dataclass
class FakeUaClient:
    endpoint: str
    connect_outcomes: list[BaseException | None]  # None = success, Exception = raise
    load_types_outcomes: list[BaseException | None] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    user: str | None = None
    password: str | None = None
    security_string: str | None = None

    async def connect(self) -> None:
        outcome = self.connect_outcomes.pop(0) if self.connect_outcomes else None
        self.events.append("connect")
        if isinstance(outcome, BaseException):
            raise outcome

    async def disconnect(self) -> None:
        self.events.append("disconnect")

    def set_user(self, username: str) -> None:
        self.user = username

    def set_password(self, password: str) -> None:
        self.password = password

    async def set_security_string(self, spec: str) -> None:
        self.security_string = spec

    async def load_data_type_definitions(self) -> dict[str, type]:
        outcome = self.load_types_outcomes.pop(0) if self.load_types_outcomes else None
        self.events.append("load_types")
        if isinstance(outcome, BaseException):
            raise outcome
        return {}

    async def connect_and_get_server_endpoints(self) -> list[Any]:
        # Unit tests don't drive discovery; return empty list.
        return []


@dataclass
class _ClientScript:
    connect: list[BaseException | None] = field(default_factory=list)
    load_types: list[BaseException | None] = field(default_factory=list)


class _Factory:
    """Keeps a reference to each FakeUaClient it hands out so tests can inspect them."""

    def __init__(self, script: list[_ClientScript]) -> None:
        self._script = script
        self.created: list[FakeUaClient] = []

    def __call__(self, endpoint: str) -> UaClientLike:
        spec = self._script.pop(0) if self._script else _ClientScript()
        fake = FakeUaClient(
            endpoint=endpoint,
            connect_outcomes=list(spec.connect),
            load_types_outcomes=list(spec.load_types),
        )
        self.created.append(fake)
        return fake  # type: ignore[return-value]


def _script(*specs: _ClientScript | list[BaseException | None]) -> list[_ClientScript]:
    out: list[_ClientScript] = []
    for s in specs:
        if isinstance(s, list):
            out.append(_ClientScript(connect=s))
        else:
            out.append(s)
    return out


async def _noop_load_enums(client: UaClientLike) -> None:
    return None


def _cfg(
    *,
    channel: Any = None,
    user: Any = None,
    backoff_ms: list[int] | None = None,
) -> ConnectionConfig:
    return ConnectionConfig(
        name="conn_test",
        endpoint="opc.tcp://mac:62541/Quickstarts/ReferenceServer",
        channel=channel or ChannelNone(),
        user=user or AnonymousUser(),
        reconnect=ReconnectConfig(backoff_ms=backoff_ms or [10]),
    )


async def test_first_attempt_success_connects_and_loads_types() -> None:
    factory = _Factory(_script([None]))  # single client; connect succeeds
    conn = AsyncuaConnection(_cfg(), client_factory=factory, load_enums=_noop_load_enums)
    await conn.start()
    await conn.wait_connected()

    assert conn.connected
    assert factory.created[0].events == ["connect", "load_types"]
    await conn.stop()


async def test_reconnect_uses_exponential_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    # Fail 3 times, then succeed on the 4th. Each client is created fresh per attempt.
    err = ConnectionRefusedError("down")
    factory = _Factory(_script([err], [err], [err], [None]))

    observed_delays: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds: float) -> None:
        observed_delays.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr("i3xua.adapters.asyncua.connection.asyncio.sleep", fake_sleep)

    conn = AsyncuaConnection(_cfg(), client_factory=factory, load_enums=_noop_load_enums)
    await conn.start()
    await conn.wait_connected()

    # Three failures -> three sleeps matching the first three backoff slots.
    # _cfg() sets backoff_ms=[10] (0.01 s); one slot → flat retry.
    assert observed_delays == [0.01] * 3
    assert len(factory.created) == 4
    assert factory.created[-1].events == ["connect", "load_types"]
    await conn.stop()


async def test_connection_lost_triggers_reconnect() -> None:
    factory = _Factory(_script([None], [None]))
    conn = AsyncuaConnection(_cfg(), client_factory=factory, load_enums=_noop_load_enums)
    connected_fired: list[int] = []
    disconnected_fired: list[int] = []

    async def on_c(client: UaClientLike) -> None:
        connected_fired.append(1)

    async def on_d(exc: Exception | None) -> None:
        disconnected_fired.append(1)

    conn.on_connected = on_c
    conn.on_disconnected = on_d

    await conn.start()
    await conn.wait_connected()
    first = conn.client
    await conn.connection_lost(ConnectionResetError("peer reset"))
    await conn.wait_connected()

    assert sum(connected_fired) == 2
    assert sum(disconnected_fired) == 1
    assert conn.client is not first
    await conn.stop()


async def test_load_types_failure_is_non_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    """`load_data_type_definitions` failures are demoted to a warning —
    some reference servers expose UA 1.04 types asyncua can't generate,
    and we'd rather keep a working session than tear it down."""
    err = RuntimeError("type load boom")
    factory = _Factory(_script(_ClientScript(connect=[None], load_types=[err])))

    real_sleep = asyncio.sleep

    async def fake_sleep(seconds: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr("i3xua.adapters.asyncua.connection.asyncio.sleep", fake_sleep)

    conn = AsyncuaConnection(_cfg(), client_factory=factory, load_enums=_noop_load_enums)
    await conn.start()
    await conn.wait_connected()

    # One client: connect succeeded, load_types raised but was swallowed.
    assert conn.connected
    assert factory.created[0].events == ["connect", "load_types"]
    await conn.stop()
