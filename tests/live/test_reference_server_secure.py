"""Live tests for anonymous-over-encrypted connections to the OPC Foundation
Quickstart Reference Server.

Gated with `@pytest.mark.live` so CI (which doesn't have the server) skips
them. Run locally with:

    uv run pytest -m live -k anonymous_over_encrypted

Prerequisites:
 - The Reference Server is listening at `OPCUA_TEST_ENDPOINT`
   (default `opc.tcp://localhost:62541/Quickstarts/ReferenceServer`).
 - The server must have its Basic256Sha256 SignAndEncrypt endpoint enabled
   (the default Reference Server configuration does this out of the box).
 - The Reference Server's PKI trust store must be accessible at the path
   reported by `%LocalApplicationData%/OPC Foundation/pki/trusted` so the
   fixture can briefly add the one-shot client cert to the server's trust
   list and clean up afterward.
"""

from __future__ import annotations

import datetime
import hashlib
import shutil
import sys
from collections.abc import Generator
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

# OPC Foundation Reference Server PKI layout on macOS.
# %LocalApplicationData% → ~/Library/Application Support on macOS.
_OPC_PKI_TRUSTED = (
    Path.home() / "Library" / "Application Support" / "OPC Foundation" / "pki" / "trusted" / "certs"
)

# The ApplicationUri embedded in the self-signed test client cert.
# Must match client.application_uri so asyncua passes its own cert check.
_APP_URI = "urn:i3xua-test"


@pytest.fixture
def client_cert_pair(tmp_path: Path) -> Generator[tuple[Path, Path], None, None]:
    """Generate a one-shot self-signed RSA cert/key for the test client.

    Automatically registers the generated cert in the Reference Server's
    trust store so `AutoAcceptUntrustedCertificates=false` is not a blocker,
    and removes it again on teardown.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "i3xua-test")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.UniformResourceIdentifier(_APP_URI)]),
            critical=False,
        )
        # OPC UA Part 6 §6.2.2 mandates these Key Usage bits for a secure-channel cert.
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=True,
                key_encipherment=True,
                data_encipherment=True,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage(
                [ExtendedKeyUsageOID.CLIENT_AUTH, ExtendedKeyUsageOID.SERVER_AUTH]
            ),
            critical=False,
        )
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    cert_der = cert.public_bytes(serialization.Encoding.DER)
    cert_path = tmp_path / "client.der"
    key_path = tmp_path / "client.pem"
    cert_path.write_bytes(cert_der)
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )

    # Register with the Reference Server's trust store so the server will
    # accept this cert. The OPC Foundation server names trust-list entries as
    # "CN [SHA1_THUMBPRINT_UPPER].der".
    thumbprint = hashlib.sha1(cert_der).hexdigest().upper()
    trust_cert_name = f"i3xua-test [{thumbprint}].der"
    _OPC_PKI_TRUSTED.mkdir(parents=True, exist_ok=True)
    trusted_cert_file = _OPC_PKI_TRUSTED / trust_cert_name
    shutil.copy2(cert_path, trusted_cert_file)

    try:
        yield cert_path, key_path
    finally:
        # Clean up — remove the one-shot cert from the server's trust store.
        trusted_cert_file.unlink(missing_ok=True)


@pytest.mark.skipif(
    sys.platform != "darwin", reason="OPC Foundation trust store path is macOS-specific"
)
@pytest.mark.live
async def test_anonymous_over_encrypted_reference_server(
    tmp_path: Path,
    client_cert_pair: tuple[Path, Path],
) -> None:
    """Connect to the Reference Server's Basic256Sha256 SignAndEncrypt endpoint
    using a self-signed client cert. Discovers the server cert, drops it into
    a trust dir, then makes the secure connection.

    Requires: Reference Server running locally with its default encrypted
    endpoint enabled. Endpoint URL comes from OPCUA_TEST_ENDPOINT env var,
    defaulting to opc.tcp://localhost:62541/Quickstarts/ReferenceServer.
    """
    import asyncio
    import os

    from asyncua import Client, ua

    from i3xua.adapters.asyncua.connection import AsyncuaConnection
    from i3xua.adapters.asyncua.upstream import _resolve_endpoint_and_trust
    from i3xua.adapters.asyncua.uri_aware import _UriAwareClient
    from i3xua.settings import (
        ConnectionConfig,
    )

    endpoint = os.environ.get(
        "OPCUA_TEST_ENDPOINT",
        "opc.tcp://localhost:62541/Quickstarts/ReferenceServer",
    )

    # 1. Discover the server cert and write it into a trust dir.
    discovery_client = Client(endpoint)
    eps = await discovery_client.connect_and_get_server_endpoints()
    encrypted_ep = next(
        ep
        for ep in eps
        if ep.SecurityMode == ua.MessageSecurityMode.SignAndEncrypt
        and ep.SecurityPolicyUri.endswith("Basic256Sha256")
    )
    trust = tmp_path / "trust"
    trust.mkdir()
    (trust / "server.der").write_bytes(encrypted_ep.ServerCertificate)

    # 2. Build the connection config.
    cert_path, key_path = client_cert_pair
    cfg = ConnectionConfig.model_validate(
        {
            "name": "live_secure",
            "endpoint": endpoint,
            "channel": {
                "mode": "SignAndEncrypt",
                "policy": "Basic256Sha256",
                "client_cert_path": str(cert_path),
                "client_key_path": str(key_path),
                "server_trust_list_dir": str(trust),
            },
            "user": {"type": "anonymous"},
            "reconnect": {"backoff_ms": [10]},
        }
    )

    # 3. Drive AsyncuaConnection end-to-end.
    async def load_enums(c: object) -> None:
        return None

    conn = AsyncuaConnection(
        cfg,
        client_factory=lambda ep: _UriAwareClient(url=ep, timeout=60),
        load_enums=load_enums,
    )

    async def pre(client: object) -> Path | None:
        # Set application_uri BEFORE _apply_channel so asyncua's internal cert
        # check (check_certificate(cert, self.application_uri, ...)) matches
        # the SAN URI we embedded in the self-signed cert above.
        client.application_uri = _APP_URI  # type: ignore[attr-defined]
        endpoints = await client.connect_and_get_server_endpoints()  # type: ignore[attr-defined]
        server_uri, resolved = _resolve_endpoint_and_trust(endpoints, cfg)
        client._override_server_uri = server_uri  # type: ignore[attr-defined]
        return resolved

    conn.on_pre_connect = pre

    await conn.start()
    try:
        await asyncio.wait_for(conn.wait_connected(), timeout=15.0)
        assert conn.connected
    finally:
        await conn.stop()
