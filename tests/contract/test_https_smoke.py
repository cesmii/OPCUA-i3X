"""Real-uvicorn HTTPS smoke test.

Generates a self-signed cert, starts uvicorn on an ephemeral port in a
thread, and verifies a TLS GET to /healthz returns 200. Exercises the full
ServerTLS → _build_uvicorn_config → uvicorn ssl_* kwargs path end-to-end.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import socket
import threading
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI

from i3xua.cli import _build_uvicorn_config

pytestmark = pytest.mark.contract


def _self_signed(tmp_path: Path) -> tuple[Path, Path]:
    """Emit a self-signed cert + unencrypted private key for 127.0.0.1."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "localhost")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(__import__("ipaddress").ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "server.crt"
    key_path = tmp_path / "server.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def tls_server(tmp_path: Path) -> Iterator[tuple[int, Path]]:
    cert_path, key_path = _self_signed(tmp_path)
    port = _free_port()

    app = FastAPI()

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # Build a minimal cfg-like object that _build_uvicorn_config can consume.
    # _build_uvicorn_config only reads cfg.server.{host,port,tls} and
    # cfg.logging.level — a SimpleNamespace covers both cleanly.
    from types import SimpleNamespace

    from i3xua.settings import ServerConfig

    server_cfg = ServerConfig.model_validate(
        {
            "host": "127.0.0.1",
            "port": port,
            "tls": {"cert_path": str(cert_path), "key_path": str(key_path)},
        }
    )
    cfg = SimpleNamespace(server=server_cfg, logging=SimpleNamespace(level="WARNING"))

    uconfig = _build_uvicorn_config(cfg, app)  # type: ignore[arg-type]
    server = uvicorn.Server(uconfig)

    def _serve() -> None:
        asyncio.run(server.serve())

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    # Wait for the server to claim the port.
    import time

    for _ in range(50):
        if server.started:
            break
        time.sleep(0.1)
    assert server.started, "uvicorn did not start in time"

    try:
        yield port, cert_path
    finally:
        server.should_exit = True
        thread.join(timeout=5)


async def test_https_smoke_get_healthz(tls_server: tuple[int, Path]) -> None:
    import ssl

    port, cert_path = tls_server
    # httpx deprecated `verify=<str>` in favour of an explicit SSLContext —
    # build one trusting only our self-signed cert so the call still proves
    # a real handshake (not `verify=False`).
    ssl_ctx = ssl.create_default_context(cafile=str(cert_path))
    async with httpx.AsyncClient(verify=ssl_ctx) as client:
        resp = await client.get(f"https://127.0.0.1:{port}/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
