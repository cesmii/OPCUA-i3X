"""Security-focused unit tests for the asyncua adapter."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from asyncua import ua

from i3xua.adapters.asyncua.connection import (
    _apply_channel,
    _apply_user_identity,
)
from i3xua.adapters.asyncua.trust import (
    TrustListMissError,
    resolve_server_cert,
)
from i3xua.adapters.asyncua.uri_aware import pick_matching_endpoint
from i3xua.settings import (
    AnonymousUser,
    ChannelNone,
    ChannelSigned,
    UsernameUser,
    X509User,
)


@dataclass
class _Ep:
    SecurityPolicyUri: str
    SecurityMode: Any
    Server: Any = None
    ServerCertificate: bytes = b""


def _ep(policy_tail: str, mode: Any) -> _Ep:
    return _Ep(
        SecurityPolicyUri=f"http://opcfoundation.org/UA/SecurityPolicy#{policy_tail}",
        SecurityMode=mode,
    )


def test_pick_matching_endpoint_none() -> None:
    eps = [_ep("None", ua.MessageSecurityMode.None_)]
    match = pick_matching_endpoint(eps, policy="None", mode=ua.MessageSecurityMode.None_)
    assert match is eps[0]


def test_pick_matching_endpoint_basic256sha256_signandencrypt() -> None:
    eps = [
        _ep("None", ua.MessageSecurityMode.None_),
        _ep("Basic256Sha256", ua.MessageSecurityMode.Sign),
        _ep("Basic256Sha256", ua.MessageSecurityMode.SignAndEncrypt),
    ]
    match = pick_matching_endpoint(
        eps,
        policy="Basic256Sha256",
        mode=ua.MessageSecurityMode.SignAndEncrypt,
    )
    assert match is eps[2]


def test_pick_matching_endpoint_aes256_no_match() -> None:
    eps = [_ep("Basic256Sha256", ua.MessageSecurityMode.SignAndEncrypt)]
    match = pick_matching_endpoint(
        eps,
        policy="Aes256Sha256RsaPss",
        mode=ua.MessageSecurityMode.SignAndEncrypt,
    )
    assert match is None


@pytest.mark.parametrize(
    "policy",
    ["Basic256Sha256", "Aes128Sha256RsaOaep", "Aes256Sha256RsaPss"],
)
def test_pick_matching_endpoint_three_policies(policy: str) -> None:
    eps = [_ep(policy, ua.MessageSecurityMode.SignAndEncrypt)]
    match = pick_matching_endpoint(eps, policy=policy, mode=ua.MessageSecurityMode.SignAndEncrypt)
    assert match is eps[0]


@pytest.mark.parametrize(
    ("config_policy", "uri_tail"),
    [
        # Real-server URI tails: AES policies are advertised with underscores per
        # OPC UA spec, while Basic256Sha256 keeps the legacy underscoreless form.
        # Our config literal is underscoreless for all three (matches asyncua's
        # set_security_string format); the matcher must tolerate either side.
        ("Aes128Sha256RsaOaep", "Aes128_Sha256_RsaOaep"),
        ("Aes256Sha256RsaPss", "Aes256_Sha256_RsaPss"),
        ("Basic256Sha256", "Basic256Sha256"),
    ],
)
def test_pick_matching_endpoint_tolerates_underscore_uri_tails(
    config_policy: str, uri_tail: str
) -> None:
    """Real OPC UA servers advertise the AES policies with underscores in the
    URI tail (`#Aes256_Sha256_RsaPss`) per OPC UA Part 7. Our config literal
    is underscoreless. The matcher must bridge the two.
    """
    eps = [_ep(uri_tail, ua.MessageSecurityMode.SignAndEncrypt)]
    match = pick_matching_endpoint(
        eps, policy=config_policy, mode=ua.MessageSecurityMode.SignAndEncrypt
    )
    assert match is eps[0]


class _CapClient:
    def __init__(self) -> None:
        self.security_string: str | None = None
        self.user: str | None = None
        self.password: str | None = None
        self.loaded_user_cert: str | None = None
        self.loaded_user_key: str | None = None
        self.application_uri: str = "urn:default:asyncua"

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def set_security_string(self, spec: str) -> None:
        self.security_string = spec

    def set_user(self, username: str) -> None:
        self.user = username

    def set_password(self, password: str) -> None:
        self.password = password

    async def load_client_certificate(self, path: str, extension: str | None = None) -> None:
        self.loaded_user_cert = path

    async def load_private_key(
        self, path: str, password: Any = None, extension: str | None = None
    ) -> None:
        self.loaded_user_key = path

    async def load_data_type_definitions(self) -> dict[str, type] | None:
        return None

    async def connect_and_get_server_endpoints(self) -> list[Any]:
        return []


def _signed(tmp_path: Path, policy: str = "Basic256Sha256") -> ChannelSigned:
    cert = tmp_path / "client.der"
    key = tmp_path / "client.pem"
    trust = tmp_path / "trust"
    cert.write_bytes(b"x")
    key.write_bytes(b"x")
    trust.mkdir()
    return ChannelSigned.model_validate(
        {
            "mode": "SignAndEncrypt",
            "policy": policy,
            "client_cert_path": str(cert),
            "client_key_path": str(key),
            "server_trust_list_dir": str(trust),
        }
    )


def test_apply_channel_none_is_noop() -> None:
    client = _CapClient()
    asyncio.run(_apply_channel(client, ChannelNone(), resolved_server_cert=None))
    assert client.security_string is None


def test_apply_channel_signed_builds_security_string(tmp_path: Path) -> None:
    client = _CapClient()
    channel = _signed(tmp_path)
    server_cert = tmp_path / "resolved_server.der"
    server_cert.write_bytes(b"y")
    asyncio.run(_apply_channel(client, channel, resolved_server_cert=server_cert))
    assert client.security_string == (
        f"Basic256Sha256,SignAndEncrypt,"
        f"{channel.client_cert_path},{channel.client_key_path},{server_cert}"
    )


def test_apply_channel_signed_requires_resolved_server_cert(tmp_path: Path) -> None:
    client = _CapClient()
    with pytest.raises(ValueError):
        asyncio.run(_apply_channel(client, _signed(tmp_path), resolved_server_cert=None))


def _real_cert_with_san(tmp_path: Path, san_uri: str) -> tuple[Path, Path]:
    """Generate a real DER-encoded RSA cert with the given SAN URI."""
    import datetime as _dt

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    now = _dt.datetime.now(_dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + _dt.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.UniformResourceIdentifier(san_uri)]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "client.der"
    key_path = tmp_path / "client.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.DER))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def test_extract_san_uri_returns_first_uri_entry(tmp_path: Path) -> None:
    """_extract_san_uri pulls the URI from a real DER-encoded cert."""
    from i3xua.adapters.asyncua.connection import _extract_san_uri

    cert_path, _ = _real_cert_with_san(tmp_path, "urn:i3xua-extract-test")
    assert _extract_san_uri(cert_path) == "urn:i3xua-extract-test"


def test_extract_san_uri_returns_none_for_cert_without_san(tmp_path: Path) -> None:
    """No SAN extension → None (not a crash)."""
    import datetime as _dt

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    from i3xua.adapters.asyncua.connection import _extract_san_uri

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "no-san")])
    now = _dt.datetime.now(_dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + _dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "no_san.der"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.DER))
    assert _extract_san_uri(cert_path) is None


def test_apply_channel_signed_stamps_application_uri_from_cert_san(tmp_path: Path) -> None:
    """_apply_channel sets client.application_uri = cert SAN URI before
    set_security_string so strict servers don't reject CreateSession with
    BadCertificateUriInvalid."""
    cert_path, key_path = _real_cert_with_san(tmp_path, "urn:i3xua")
    trust = tmp_path / "trust"
    trust.mkdir()
    channel = ChannelSigned.model_validate(
        {
            "mode": "SignAndEncrypt",
            "policy": "Basic256Sha256",
            "client_cert_path": str(cert_path),
            "client_key_path": str(key_path),
            "server_trust_list_dir": str(trust),
        }
    )
    server_cert = tmp_path / "server.der"
    server_cert.write_bytes(b"\x30y")
    client = _CapClient()
    assert client.application_uri == "urn:default:asyncua"  # default before
    asyncio.run(_apply_channel(client, channel, resolved_server_cert=server_cert))
    assert client.application_uri == "urn:i3xua"  # stamped after


def test_apply_user_identity_anonymous_is_noop() -> None:
    client = _CapClient()
    asyncio.run(_apply_user_identity(client, AnonymousUser()))
    assert client.user is None
    assert client.loaded_user_cert is None


def test_apply_user_identity_username() -> None:
    client = _CapClient()
    user = UsernameUser(type="username", username="u", password="p")
    asyncio.run(_apply_user_identity(client, user))
    assert client.user == "u"
    assert client.password == "p"


def test_apply_user_identity_x509(tmp_path: Path) -> None:
    cert = tmp_path / "u.der"
    key = tmp_path / "u.pem"
    cert.write_bytes(b"x")
    key.write_bytes(b"x")
    user = X509User(type="x509", cert_path=cert, key_path=key)
    client = _CapClient()
    asyncio.run(_apply_user_identity(client, user))
    assert client.loaded_user_cert == str(cert)
    assert client.loaded_user_key == str(key)


# ---------------------------------------------------------------------------
# Trust-list resolver (Task 6)
# ---------------------------------------------------------------------------

_DER_A = b"\x30\x82\x01\x01" + b"a" * 32
_DER_B = b"\x30\x82\x01\x01" + b"b" * 32


def _pem(der: bytes) -> bytes:
    import base64

    body = base64.encodebytes(der)
    return b"-----BEGIN CERTIFICATE-----\n" + body + b"-----END CERTIFICATE-----\n"


def test_resolve_server_cert_matches_der(tmp_path: Path) -> None:
    a = tmp_path / "a.der"
    b = tmp_path / "b.der"
    a.write_bytes(_DER_A)
    b.write_bytes(_DER_B)
    match = resolve_server_cert(server_cert_der=_DER_A, trust_dir=tmp_path)
    assert match == a


def test_resolve_server_cert_matches_pem(tmp_path: Path) -> None:
    p = tmp_path / "a.pem"
    p.write_bytes(_pem(_DER_A))
    (tmp_path / "irrelevant.txt").write_text("hello")
    match = resolve_server_cert(server_cert_der=_DER_A, trust_dir=tmp_path)
    assert match == p


def test_resolve_server_cert_no_match_raises(tmp_path: Path) -> None:
    (tmp_path / "a.der").write_bytes(_DER_A)
    with pytest.raises(TrustListMissError):
        resolve_server_cert(server_cert_der=_DER_B, trust_dir=tmp_path)


def test_resolve_server_cert_empty_dir_raises(tmp_path: Path) -> None:
    with pytest.raises(TrustListMissError):
        resolve_server_cert(server_cert_der=_DER_A, trust_dir=tmp_path)


def test_resolve_server_cert_skips_non_cert_files(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("ignore me")
    (tmp_path / "a.der").write_bytes(_DER_A)
    match = resolve_server_cert(server_cert_der=_DER_A, trust_dir=tmp_path)
    assert match == tmp_path / "a.der"


# ---------------------------------------------------------------------------
# _resolve_endpoint_and_trust (Task 7)
# ---------------------------------------------------------------------------

from i3xua.adapters.asyncua.upstream import (  # noqa: E402
    _resolve_endpoint_and_trust,
)
from i3xua.settings import ConnectionConfig  # noqa: E402


@dataclass
class _FakeServer:
    ApplicationUri: str = "urn:fake:app"


@dataclass
class _FakeEp:
    SecurityPolicyUri: str
    SecurityMode: Any
    Server: _FakeServer
    ServerCertificate: bytes


def _conn_signed(tmp_path: Path, *, policy: str = "Basic256Sha256") -> ConnectionConfig:
    cert = tmp_path / "client.der"
    key = tmp_path / "client.pem"
    trust = tmp_path / "trust"
    cert.write_bytes(b"x")
    key.write_bytes(b"x")
    trust.mkdir()
    return ConnectionConfig.model_validate(
        {
            "name": "c1",
            "endpoint": "opc.tcp://x:4840/foo",
            "channel": {
                "mode": "SignAndEncrypt",
                "policy": policy,
                "client_cert_path": str(cert),
                "client_key_path": str(key),
                "server_trust_list_dir": str(trust),
            },
            "user": {"type": "anonymous"},
        }
    )


def test_resolve_endpoint_and_trust_happy(tmp_path: Path) -> None:
    cfg = _conn_signed(tmp_path)
    assert isinstance(cfg.channel, ChannelSigned)
    server_der = b"\x30hello-server"
    (cfg.channel.server_trust_list_dir / "server.der").write_bytes(server_der)
    eps = [
        _FakeEp(
            SecurityPolicyUri=("http://opcfoundation.org/UA/SecurityPolicy#Basic256Sha256"),
            SecurityMode=ua.MessageSecurityMode.SignAndEncrypt,
            Server=_FakeServer(ApplicationUri="urn:real:app"),
            ServerCertificate=server_der,
        )
    ]
    server_uri, resolved = _resolve_endpoint_and_trust(eps, cfg)
    assert server_uri == "urn:real:app"
    assert resolved == cfg.channel.server_trust_list_dir / "server.der"


def test_resolve_endpoint_and_trust_no_endpoint_match(tmp_path: Path) -> None:
    cfg = _conn_signed(tmp_path, policy="Aes256Sha256RsaPss")
    eps = [
        _FakeEp(
            SecurityPolicyUri=("http://opcfoundation.org/UA/SecurityPolicy#Basic256Sha256"),
            SecurityMode=ua.MessageSecurityMode.SignAndEncrypt,
            Server=_FakeServer(),
            ServerCertificate=b"x",
        )
    ]
    with pytest.raises(RuntimeError, match="no endpoint"):
        _resolve_endpoint_and_trust(eps, cfg)


def test_resolve_endpoint_and_trust_no_trust_match(tmp_path: Path) -> None:
    cfg = _conn_signed(tmp_path)
    assert isinstance(cfg.channel, ChannelSigned)
    (cfg.channel.server_trust_list_dir / "different.der").write_bytes(b"\x30other")
    eps = [
        _FakeEp(
            SecurityPolicyUri=("http://opcfoundation.org/UA/SecurityPolicy#Basic256Sha256"),
            SecurityMode=ua.MessageSecurityMode.SignAndEncrypt,
            Server=_FakeServer(),
            ServerCertificate=b"\x30not-trusted",
        )
    ]
    with pytest.raises(TrustListMissError):
        _resolve_endpoint_and_trust(eps, cfg)


def test_resolve_endpoint_and_trust_autotrusts_when_dir_omitted(tmp_path: Path) -> None:
    """When `server_trust_list_dir` is omitted, the discovered cert is the
    identity — auto-trusted to a per-connection tempdir cache and returned."""
    cert = tmp_path / "client.der"
    key = tmp_path / "client.pem"
    cert.write_bytes(b"x")
    key.write_bytes(b"x")
    cfg = ConnectionConfig.model_validate(
        {
            "name": "kepware_test",
            "endpoint": "opc.tcp://x:4840/foo",
            "channel": {
                "mode": "SignAndEncrypt",
                "policy": "Basic256Sha256",
                "client_cert_path": str(cert),
                "client_key_path": str(key),
                # NOTE: no server_trust_list_dir
            },
            "user": {"type": "anonymous"},
        }
    )
    server_der = b"\x30auto-trusted-server"
    eps = [
        _FakeEp(
            SecurityPolicyUri="http://opcfoundation.org/UA/SecurityPolicy#Basic256Sha256",
            SecurityMode=ua.MessageSecurityMode.SignAndEncrypt,
            Server=_FakeServer(ApplicationUri="urn:auto"),
            ServerCertificate=server_der,
        )
    ]
    server_uri, resolved = _resolve_endpoint_and_trust(eps, cfg)
    assert server_uri == "urn:auto"
    assert resolved is not None
    assert resolved.read_bytes() == server_der
    assert "kepware_test" in str(resolved)


def test_resolve_endpoint_and_trust_channel_none(tmp_path: Path) -> None:
    cfg = ConnectionConfig.model_validate(
        {
            "name": "c1",
            "endpoint": "opc.tcp://x:4840/foo",
            "channel": {"mode": "None"},
            "user": {"type": "anonymous"},
        }
    )
    eps = [
        _FakeEp(
            SecurityPolicyUri="http://opcfoundation.org/UA/SecurityPolicy#None",
            SecurityMode=ua.MessageSecurityMode.None_,
            Server=_FakeServer(ApplicationUri="urn:fake"),
            ServerCertificate=b"",
        )
    ]
    server_uri, resolved = _resolve_endpoint_and_trust(eps, cfg)
    assert server_uri == "urn:fake"
    assert resolved is None


# ---------------------------------------------------------------------------
# _connect_once threads resolved cert into _apply_channel (Task 8)
# ---------------------------------------------------------------------------

from i3xua.adapters.asyncua.connection import AsyncuaConnection  # noqa: E402


class _StubClient:
    """Minimal client used to drive _connect_once end-to-end with a stubbed pre-connect."""

    def __init__(self) -> None:
        self.security_string: str | None = None
        self.user: str | None = None
        self.password: str | None = None
        self.connect_called = False
        self.connection_lost_callback: Any = None

    async def connect(self) -> None:
        self.connect_called = True

    async def disconnect(self) -> None:
        pass

    def set_user(self, username: str) -> None:
        self.user = username

    def set_password(self, password: str) -> None:
        self.password = password

    async def set_security_string(self, spec: str) -> None:
        self.security_string = spec

    async def load_client_certificate(self, path: str, extension: str | None = None) -> None:
        pass

    async def load_private_key(
        self, path: str, password: Any = None, extension: str | None = None
    ) -> None:
        pass

    async def load_data_type_definitions(self) -> dict[str, type]:
        return {}

    async def connect_and_get_server_endpoints(self) -> list[Any]:
        return []


async def _noop_load_enums(client: Any) -> None:
    return None


def test_connect_once_threads_resolved_cert_into_security_string(tmp_path: Path) -> None:
    cfg = _conn_signed(tmp_path)
    assert isinstance(cfg.channel, ChannelSigned)
    server_cert = tmp_path / "trust" / "server.der"
    server_cert.write_bytes(b"\x30server")

    captured: dict[str, Any] = {}

    def factory(endpoint: str) -> Any:
        captured["client"] = _StubClient()
        return captured["client"]

    async def fake_pre_connect(client: Any) -> Path | None:
        return server_cert

    conn = AsyncuaConnection(
        cfg,
        client_factory=factory,
        load_enums=_noop_load_enums,
    )
    conn.on_pre_connect = fake_pre_connect

    async def run() -> None:
        await conn.start()
        await conn.wait_connected()
        await conn.stop()

    asyncio.run(run())

    expected = (
        f"Basic256Sha256,SignAndEncrypt,"
        f"{cfg.channel.client_cert_path},{cfg.channel.client_key_path},{server_cert}"
    )
    assert captured["client"].security_string == expected
