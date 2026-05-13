"""OPC UA connection lifecycle: connect, auth, load types, reconnect.

Kept generic over the actual client via the `UaClientLike` Protocol — the
production path binds to `asyncua.Client`, tests substitute a fake. That split
is the only concession to testability; the public API stays in neutral terms.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

from cryptography import x509

from i3xua.settings import (
    AnonymousUser,
    ChannelNone,
    ChannelSigned,
    ConnectionConfig,
    UsernameUser,
    X509User,
)

logger = logging.getLogger(__name__)

BACKOFF_SEQUENCE = (5.0,)


class UaClientLike(Protocol):
    application_uri: str  # set on the client BEFORE connect to match cert SAN URI

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    def set_user(self, username: str) -> None: ...
    def set_password(self, password: str) -> None: ...
    async def set_security_string(self, spec: str) -> None: ...
    async def load_client_certificate(self, path: str, extension: str | None = None) -> None: ...
    async def load_private_key(
        self, path: str, password: Any = None, extension: str | None = None
    ) -> None: ...
    async def load_data_type_definitions(self) -> dict[str, type] | None: ...
    async def connect_and_get_server_endpoints(self) -> list[Any]: ...


ClientFactory = Callable[[str], UaClientLike]
"""Takes an endpoint URL and returns an unconnected client instance."""

LoadEnums = Callable[[UaClientLike], Awaitable[Any]]
"""Separate function in asyncua so we inject it independently for tests."""


def _extract_san_uri(cert_path: Path) -> str | None:
    """Return the first URI entry from the cert's SubjectAlternativeName, or None.

    OPC UA Part 6 §6.2.2 mandates that the client cert's SAN carry an
    ``ApplicationUri``-shaped URI. Strict servers reject CreateSessionRequests
    where the request's ``ClientDescription.ApplicationUri`` doesn't match
    the cert's SAN URI (``BadCertificateUriInvalid``). We extract the cert's
    SAN URI here and stamp it onto ``client.application_uri`` before
    ``set_security_string`` so asyncua sends a matching ApplicationUri.
    """
    raw = cert_path.read_bytes()
    cert = (
        x509.load_pem_x509_certificate(raw)
        if raw.lstrip().startswith(b"-----BEGIN")
        else x509.load_der_x509_certificate(raw)
    )
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return None
    for entry in san:
        if isinstance(entry, x509.UniformResourceIdentifier):
            return entry.value
    return None


async def _apply_channel(
    client: UaClientLike,
    channel: ChannelNone | ChannelSigned,
    *,
    resolved_server_cert: Path | None,
) -> None:
    if isinstance(channel, ChannelNone):
        return
    if resolved_server_cert is None:
        raise ValueError(
            "ChannelSigned requires a resolved server certificate path "
            "(must come from trust-list resolution in _pre_connect)"
        )
    # Match asyncua's ApplicationUri to the cert's SAN URI so strict servers
    # don't reject CreateSession with BadCertificateUriInvalid.
    san_uri = _extract_san_uri(channel.client_cert_path)
    if san_uri is not None:
        client.application_uri = san_uri  # type: ignore[attr-defined]
    spec = ",".join(
        [
            channel.policy,
            channel.mode,
            str(channel.client_cert_path),
            str(channel.client_key_path),
            str(resolved_server_cert),
        ]
    )
    await client.set_security_string(spec)


async def _apply_user_identity(
    client: UaClientLike,
    user: AnonymousUser | UsernameUser | X509User,
) -> None:
    if isinstance(user, AnonymousUser):
        return
    if isinstance(user, UsernameUser):
        client.set_user(user.username)
        client.set_password(user.password)
        return
    if isinstance(user, X509User):
        # asyncua's load_client_certificate populates client.user_certificate
        # — despite the misleading name it's the USER token cert, not the
        # channel cert. Channel cert is set via set_security_string above.
        await client.load_client_certificate(str(user.cert_path))
        await client.load_private_key(str(user.key_path))
        return
    raise TypeError(f"unsupported user identity: {user!r}")


class AsyncuaConnection:
    """Owns one `asyncua.Client`. Lives inside a worker thread's event loop.

    Lifecycle:
      - start(): attempt initial connect; on failure loops with backoff until stopped.
      - connected_event: set whenever the client has a live session + types loaded.
      - on_connected / on_disconnected: hooks fired from inside the worker loop
        so downstream components (browse, subscribe) can (re)install state.
    """

    __slots__ = (
        "_backoff_seconds",
        "_cfg",
        "_client",
        "_client_factory",
        "_closed",
        "_connected_event",
        "_load_enums",
        "_reconnect_task",
        "on_connected",
        "on_disconnected",
        "on_pre_connect",
    )

    def __init__(
        self,
        cfg: ConnectionConfig,
        *,
        client_factory: ClientFactory,
        load_enums: LoadEnums,
    ) -> None:
        self._cfg = cfg
        self._client_factory = client_factory
        self._load_enums = load_enums
        self._client: UaClientLike | None = None
        self._closed = False
        self._connected_event = asyncio.Event()
        self._reconnect_task: asyncio.Task[None] | None = None
        # per-connection backoff sequence, in seconds. Falls back to the
        # module-level BACKOFF_SEQUENCE if the config didn't supply one.
        self._backoff_seconds: tuple[float, ...] = (
            tuple(ms / 1000.0 for ms in cfg.reconnect.backoff_ms)
            if cfg.reconnect.backoff_ms
            else BACKOFF_SEQUENCE
        )
        self.on_connected: Callable[[UaClientLike], Awaitable[None]] | None = None
        self.on_disconnected: Callable[[Exception | None], Awaitable[None]] | None = None
        # invoked with the newly-constructed client before _apply_channel /
        # _apply_user_identity, so the production path can discover the server's
        # ApplicationUri and stash it on the client for the upcoming CreateSessionRequest.
        self.on_pre_connect: Callable[[UaClientLike], Awaitable[Path | None]] | None = None

    @property
    def name(self) -> str:
        return self._cfg.name

    @property
    def client(self) -> UaClientLike:
        if self._client is None:
            raise RuntimeError(f"connection {self.name!r} not currently connected")
        return self._client

    @property
    def connected(self) -> bool:
        return self._connected_event.is_set()

    async def wait_connected(self) -> None:
        await self._connected_event.wait()

    async def start(self) -> None:
        self._start_reconnect_loop()

    async def stop(self) -> None:
        self._closed = True
        task = self._reconnect_task
        self._reconnect_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await self._disconnect(None)

    async def connection_lost(self, exc: Exception | None) -> None:
        """Called by asyncua from inside `_monitor_server_task` when the
        channel drops. MUST NOT call `client.disconnect()` (see `_disconnect`
        docstring for why). Clears local state and spins a reconnect loop."""
        logger.warning("opcua connection lost: %s (%s)", self.name, exc)
        await self._disconnect(exc)
        logger.info("reconnect: starting reconnect loop for %s", self.name)
        self._start_reconnect_loop()

    def _start_reconnect_loop(self) -> None:
        if self._closed:
            logger.debug("reconnect skipped for %s: closed", self.name)
            return
        task = self._reconnect_task
        if task is not None and not task.done():
            logger.debug("reconnect skipped for %s: loop already running", self.name)
            return
        self._reconnect_task = asyncio.create_task(
            self._run_reconnect_loop(), name=f"opcua-{self.name}-reconnect"
        )

    async def _run_reconnect_loop(self) -> None:
        attempt = 0
        logger.debug("reconnect loop entered for %s", self.name)
        try:
            while not self._closed:
                try:
                    await self._connect_once()
                    logger.info("reconnect: %s connected successfully", self.name)
                    return
                except asyncio.CancelledError:
                    logger.debug("reconnect loop cancelled for %s", self.name)
                    raise
                except Exception as exc:
                    seq = self._backoff_seconds
                    delay = seq[min(attempt, len(seq) - 1)]
                    logger.warning(
                        "reconnect: %s attempt %d failed: %r; retry in %.1fs",
                        self.name,
                        attempt,
                        exc,
                        delay,
                    )
                    attempt += 1
                    try:
                        await asyncio.sleep(delay)
                    except asyncio.CancelledError:
                        raise
        finally:
            if self._reconnect_task is asyncio.current_task():
                self._reconnect_task = None
            logger.debug("reconnect loop exited for %s", self.name)

    async def _connect_once(self) -> None:
        client = self._client_factory(self._cfg.endpoint)
        resolved_server_cert: Path | None = None
        if self.on_pre_connect is not None:
            resolved_server_cert = await self.on_pre_connect(client)
        await _apply_channel(
            client,
            self._cfg.channel,
            resolved_server_cert=resolved_server_cert,
        )
        await _apply_user_identity(client, self._cfg.user)
        await client.connect()
        # wire asyncua's disconnect detector to our reconnect loop.
        # MUST happen after connect() (which creates _monitor_server_task).
        client.connection_lost_callback = self.connection_lost  # type: ignore[attr-defined]
        logger.debug("connection_lost_callback wired on %s", self.name)
        # `load_data_type_definitions` is best-effort: some servers expose
        # types asyncua can't yet generate (e.g. UA 1.04 types referencing the
        # legacy `ua.Enumeration` base). Failure here shouldn't break connect;
        # the browse layer still surfaces everything it can see.
        try:
            await client.load_data_type_definitions()
        except Exception as exc:
            logger.warning(
                "load_data_type_definitions failed for %s (%s); proceeding without custom types",
                self._cfg.name,
                exc,
            )
        try:
            await self._load_enums(client)
        except Exception as exc:
            logger.warning(
                "load_enums failed for %s (%s); proceeding without enum types",
                self._cfg.name,
                exc,
            )
        self._client = client
        self._connected_event.set()
        if self.on_connected is not None:
            try:
                await self.on_connected(client)
            except Exception as exc:
                # on_connected failures shouldn't tear the session down — they
                # usually reflect downstream issues (e.g. unsupported custom
                # types during address-space reflection) that can be retried
                # by a later /admin/refresh.
                logger.warning("on_connected hook raised for %s: %s", self.name, exc)

    async def _disconnect(self, exc: Exception | None) -> None:
        client = self._client
        self._client = None
        self._connected_event.clear()
        if client is not None and exc is None:
            # Clean session close only on intentional stop() (exc=None).
            # When called from connection_lost (exc != None), the session is
            # already dead. Calling client.disconnect() here would DEADLOCK:
            # connection_lost is invoked from inside asyncua's
            # _monitor_server_task, and disconnect() tries to
            # cancel + await that same task.
            with _suppress():
                await client.disconnect()
        if self.on_disconnected is not None:
            await self.on_disconnected(exc)


class _suppress:
    """Tiny contextmanager that swallows adapter-layer cleanup failures."""

    def __enter__(self) -> _suppress:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if exc is not None:
            logger.debug("suppressed during cleanup: %s", exc)
        return True
