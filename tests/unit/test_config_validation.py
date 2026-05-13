"""prove the shipped config.example.yaml parses and that each invariant
baked into AppConfig actually rejects a malformed config."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from i3xua.settings import (
    AnonymousUser,
    AppConfig,
    ChannelNone,
    ChannelSecurity,
    ChannelSigned,
    ConnectionConfig,
    ServerConfig,
    ServerTLS,
    UserIdentity,
    UsernameUser,
    X509User,
    load_config,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_example_config_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("I3XUA_TOKEN", "t-example")
    cfg = load_config(REPO_ROOT / "config.example.yaml")

    assert len(cfg.connections) == 1
    conn = cfg.connections[0]
    assert conn.name == "conn_ref"
    assert conn.endpoint.startswith("opc.tcp://")
    # subscriptions are auto-tier; config carries default intervals only.
    assert conn.default_publishing_interval_ms == 1000
    assert conn.default_sampling_interval_ms == 500

    assert cfg.server.auth.mode == "bearer"
    assert cfg.server.auth.tokens == ["t-example"]


def test_env_expansion_missing_var_becomes_empty_string(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("I3XUA_TOKEN", raising=False)
    with pytest.raises(ValidationError, match="bearer token list must not contain empty strings"):
        load_config(REPO_ROOT / "config.example.yaml")


def _base(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "connections": [
            {
                "name": "c1",
                "endpoint": "opc.tcp://host:4840",
            }
        ],
    }
    base.update(overrides)
    return base


def test_default_intervals_round_trip() -> None:
    """ConnectionConfig accepts default_*_interval_ms fields directly."""
    cfg = _base(
        connections=[
            {
                "name": "c1",
                "endpoint": "opc.tcp://host:4840",
                "default_publishing_interval_ms": 250,
                "default_sampling_interval_ms": 100,
            }
        ]
    )
    parsed = AppConfig.model_validate(cfg)
    assert parsed.connections[0].default_publishing_interval_ms == 250
    assert parsed.connections[0].default_sampling_interval_ms == 100


def test_non_opc_endpoint_is_rejected() -> None:
    cfg = _base(
        connections=[
            {
                "name": "c1",
                "endpoint": "http://not-opc",
            }
        ]
    )
    with pytest.raises(ValidationError, match=r"opc\.tcp://"):
        AppConfig.model_validate(cfg)


def test_duplicate_connection_names_rejected() -> None:
    cfg = _base(
        connections=[
            {"name": "c1", "endpoint": "opc.tcp://a:4840"},
            {"name": "c1", "endpoint": "opc.tcp://b:4840"},
        ]
    )
    with pytest.raises(ValidationError, match="connection names must be unique"):
        AppConfig.model_validate(cfg)


def test_unknown_auth_mode_rejected() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate(_base(server={"auth": {"mode": "magic"}}))


def test_bearer_empty_tokens_rejected() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate(_base(server={"auth": {"mode": "bearer", "tokens": [""]}}))


# ---------------------------------------------------------------------------
# Task 1: legacy field rejection tests (must FAIL until Task 2 ships)
# ---------------------------------------------------------------------------


def _minimal_connection() -> dict[str, object]:
    return {
        "name": "conn_a",
        "endpoint": "opc.tcp://localhost:4840",
    }


def test_legacy_threads_key_rejected_with_migration_message() -> None:
    payload = {
        "threads": [{"name": "opc_main"}],
        "connections": [_minimal_connection()],
    }
    with pytest.raises(ValidationError) as exc:
        AppConfig.model_validate(payload)
    assert "threads:" in str(exc.value)
    assert "removed" in str(exc.value).lower()


def test_legacy_thread_field_on_connection_rejected_with_migration_message() -> None:
    conn = _minimal_connection() | {"thread": "opc_main"}
    payload = {"connections": [conn]}
    with pytest.raises(ValidationError) as exc:
        AppConfig.model_validate(payload)
    assert "thread:" in str(exc.value)
    assert "removed" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# Task 1 (security hardening): ChannelSecurity discriminated union
# ---------------------------------------------------------------------------


def test_channel_none_rejects_policy_other_than_none() -> None:
    with pytest.raises(ValidationError):
        ChannelNone.model_validate({"mode": "None", "policy": "Basic256Sha256"})


def test_channel_none_rejects_cert_paths() -> None:
    with pytest.raises(ValidationError):
        ChannelNone.model_validate({"mode": "None", "client_cert_path": "/tmp/x"})


def test_channel_none_accepts_minimal() -> None:
    cfg = ChannelNone.model_validate({"mode": "None"})
    assert cfg.mode == "None"


def test_channel_signed_accepts_valid_config(tmp_path: Path) -> None:
    cert = tmp_path / "client.der"
    key = tmp_path / "client.pem"
    trust = tmp_path / "trusted"
    cert.write_bytes(b"x")
    key.write_bytes(b"x")
    trust.mkdir()

    cfg = ChannelSigned.model_validate(
        {
            "mode": "SignAndEncrypt",
            "policy": "Aes256Sha256RsaPss",
            "client_cert_path": str(cert),
            "client_key_path": str(key),
            "server_trust_list_dir": str(trust),
        }
    )
    assert cfg.policy == "Aes256Sha256RsaPss"
    assert cfg.client_cert_path == cert


def test_channel_signed_rejects_basic128rsa15(tmp_path: Path) -> None:
    cert = tmp_path / "client.der"
    key = tmp_path / "client.pem"
    trust = tmp_path / "trusted"
    cert.write_bytes(b"x")
    key.write_bytes(b"x")
    trust.mkdir()
    with pytest.raises(ValidationError):
        ChannelSigned.model_validate(
            {
                "mode": "SignAndEncrypt",
                "policy": "Basic128Rsa15",
                "client_cert_path": str(cert),
                "client_key_path": str(key),
                "server_trust_list_dir": str(trust),
            }
        )


def test_channel_signed_rejects_policy_none(tmp_path: Path) -> None:
    cert = tmp_path / "client.der"
    key = tmp_path / "client.pem"
    trust = tmp_path / "trusted"
    cert.write_bytes(b"x")
    key.write_bytes(b"x")
    trust.mkdir()
    with pytest.raises(ValidationError):
        ChannelSigned.model_validate(
            {
                "mode": "Sign",
                "policy": "None",
                "client_cert_path": str(cert),
                "client_key_path": str(key),
                "server_trust_list_dir": str(trust),
            }
        )


def test_channel_signed_rejects_missing_cert_paths(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        ChannelSigned.model_validate({"mode": "Sign", "policy": "Basic256Sha256"})


def test_channel_security_dispatches_to_channel_none() -> None:
    parsed: ChannelNone | ChannelSigned = TypeAdapter(ChannelSecurity).validate_python(
        {"mode": "None"}
    )
    assert isinstance(parsed, ChannelNone)


def test_channel_security_dispatches_to_channel_signed(tmp_path: Path) -> None:
    cert = tmp_path / "client.der"
    key = tmp_path / "client.pem"
    trust = tmp_path / "trusted"
    cert.write_bytes(b"x")
    key.write_bytes(b"x")
    trust.mkdir()
    parsed: ChannelNone | ChannelSigned = TypeAdapter(ChannelSecurity).validate_python(
        {
            "mode": "SignAndEncrypt",
            "policy": "Basic256Sha256",
            "client_cert_path": str(cert),
            "client_key_path": str(key),
            "server_trust_list_dir": str(trust),
        }
    )
    assert isinstance(parsed, ChannelSigned)


def test_channel_security_rejects_unknown_mode() -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(ChannelSecurity).validate_python({"mode": "Bogus"})


# ---------------------------------------------------------------------------
# Task 2 (security hardening): UserIdentity discriminated union
# ---------------------------------------------------------------------------


def test_anonymous_user_minimal() -> None:
    u = AnonymousUser.model_validate({"type": "anonymous"})
    assert u.type == "anonymous"


def test_username_user_requires_username_password() -> None:
    with pytest.raises(ValidationError):
        UsernameUser.model_validate({"type": "username", "username": "u"})


def test_username_user_happy_path() -> None:
    u = UsernameUser.model_validate({"type": "username", "username": "u", "password": "p"})
    assert u.password == "p"


def test_username_user_rejects_empty_username() -> None:
    with pytest.raises(ValidationError):
        UsernameUser.model_validate({"type": "username", "username": "", "password": "p"})


def test_username_user_rejects_empty_password() -> None:
    with pytest.raises(ValidationError):
        UsernameUser.model_validate({"type": "username", "username": "u", "password": ""})


def test_x509_user_requires_existing_files(tmp_path: Path) -> None:
    cert = tmp_path / "u.der"
    key = tmp_path / "u.pem"
    cert.write_bytes(b"x")
    key.write_bytes(b"x")
    u = X509User.model_validate({"type": "x509", "cert_path": str(cert), "key_path": str(key)})
    assert u.cert_path == cert


def test_x509_user_rejects_missing_files(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        X509User.model_validate(
            {
                "type": "x509",
                "cert_path": str(tmp_path / "nope.der"),
                "key_path": str(tmp_path / "nope.pem"),
            }
        )


def test_user_identity_dispatches_to_anonymous() -> None:
    parsed: AnonymousUser | UsernameUser | X509User = TypeAdapter(UserIdentity).validate_python(
        {"type": "anonymous"}
    )
    assert isinstance(parsed, AnonymousUser)


def test_user_identity_dispatches_to_username() -> None:
    parsed: AnonymousUser | UsernameUser | X509User = TypeAdapter(UserIdentity).validate_python(
        {"type": "username", "username": "u", "password": "p"}
    )
    assert isinstance(parsed, UsernameUser)


def test_user_identity_dispatches_to_x509(tmp_path: Path) -> None:
    cert = tmp_path / "u.der"
    key = tmp_path / "u.pem"
    cert.write_bytes(b"x")
    key.write_bytes(b"x")
    parsed: AnonymousUser | UsernameUser | X509User = TypeAdapter(UserIdentity).validate_python(
        {"type": "x509", "cert_path": str(cert), "key_path": str(key)}
    )
    assert isinstance(parsed, X509User)


def test_user_identity_rejects_unknown_type() -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(UserIdentity).validate_python({"type": "bogus"})


# ---------------------------------------------------------------------------
# Task 3 (security hardening): ConnectionConfig channel: + user: fields
# ---------------------------------------------------------------------------


def _conn_dict(channel: dict[str, object], user: dict[str, object]) -> dict[str, object]:
    return {
        "name": "c1",
        "endpoint": "opc.tcp://x:4840/foo",
        "channel": channel,
        "user": user,
    }


def test_connection_config_username_over_none_channel_rejected() -> None:
    with pytest.raises(ValidationError) as excinfo:
        ConnectionConfig.model_validate(
            _conn_dict(
                channel={"mode": "None"},
                user={"type": "username", "username": "u", "password": "p"},
            )
        )
    assert "cleartext" in str(excinfo.value).lower() or "none" in str(excinfo.value).lower()


def test_connection_config_anonymous_over_none_channel_ok() -> None:
    cfg = ConnectionConfig.model_validate(
        _conn_dict(channel={"mode": "None"}, user={"type": "anonymous"})
    )
    assert cfg.channel.mode == "None"
    assert cfg.user.type == "anonymous"


def test_connection_config_username_over_signed_channel_ok(tmp_path: Path) -> None:
    cert = tmp_path / "c.der"
    key = tmp_path / "k.pem"
    trust = tmp_path / "trusted"
    cert.write_bytes(b"x")
    key.write_bytes(b"x")
    trust.mkdir()

    cfg = ConnectionConfig.model_validate(
        _conn_dict(
            channel={
                "mode": "SignAndEncrypt",
                "policy": "Basic256Sha256",
                "client_cert_path": str(cert),
                "client_key_path": str(key),
                "server_trust_list_dir": str(trust),
            },
            user={"type": "username", "username": "u", "password": "p"},
        )
    )
    assert isinstance(cfg.user, UsernameUser)
    assert cfg.user.username == "u"


def test_server_tls_absent_is_default(tmp_path: Path) -> None:
    cfg = ServerConfig.model_validate({})
    assert cfg.tls is None


def test_server_tls_accepts_valid_paths(tmp_path: Path) -> None:
    cert = tmp_path / "server.crt"
    key = tmp_path / "server.key"
    cert.write_bytes(b"x")
    key.write_bytes(b"x")

    tls = ServerTLS.model_validate({"cert_path": str(cert), "key_path": str(key)})
    assert tls.cert_path == cert
    assert tls.key_path == key
    assert tls.key_password is None


def test_server_tls_accepts_key_password(tmp_path: Path) -> None:
    cert = tmp_path / "server.crt"
    key = tmp_path / "server.key"
    cert.write_bytes(b"x")
    key.write_bytes(b"x")

    tls = ServerTLS.model_validate(
        {"cert_path": str(cert), "key_path": str(key), "key_password": "hunter2"}
    )
    assert tls.key_password == "hunter2"


def test_server_tls_rejects_missing_cert(tmp_path: Path) -> None:
    key = tmp_path / "server.key"
    key.write_bytes(b"x")
    with pytest.raises(ValidationError):
        ServerTLS.model_validate({"cert_path": str(tmp_path / "nope.crt"), "key_path": str(key)})


def test_server_tls_rejects_missing_key(tmp_path: Path) -> None:
    cert = tmp_path / "server.crt"
    cert.write_bytes(b"x")
    with pytest.raises(ValidationError):
        ServerTLS.model_validate({"cert_path": str(cert), "key_path": str(tmp_path / "nope.key")})


def test_server_tls_forbids_extra_keys(tmp_path: Path) -> None:
    cert = tmp_path / "server.crt"
    key = tmp_path / "server.key"
    cert.write_bytes(b"x")
    key.write_bytes(b"x")
    with pytest.raises(ValidationError):
        ServerTLS.model_validate(
            {
                "cert_path": str(cert),
                "key_path": str(key),
                "client_ca_path": str(cert),  # mTLS not in scope; must be rejected
            }
        )


def test_server_config_tls_round_trips(tmp_path: Path) -> None:
    cert = tmp_path / "server.crt"
    key = tmp_path / "server.key"
    cert.write_bytes(b"x")
    key.write_bytes(b"x")

    cfg = ServerConfig.model_validate(
        {
            "tls": {"cert_path": str(cert), "key_path": str(key)},
        }
    )
    assert cfg.tls is not None
    assert cfg.tls.cert_path == cert


def test_server_config_tls_env_password_expands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The YAML loader runs _expand_env over nested dicts before validation,
    so ${VAR} inside server.tls.key_password resolves at load time."""
    cert = tmp_path / "server.crt"
    key = tmp_path / "server.key"
    cert.write_bytes(b"x")
    key.write_bytes(b"x")
    monkeypatch.setenv("MY_KEY_PW", "s3cret")

    yaml_text = f"""
server:
  tls:
    cert_path: {cert}
    key_path: {key}
    key_password: ${{MY_KEY_PW}}
connections:
  - name: c1
    endpoint: opc.tcp://localhost:4840
"""
    config_file = tmp_path / "test.yaml"
    config_file.write_text(yaml_text)
    cfg = load_config(config_file)
    assert cfg.server.tls is not None
    assert cfg.server.tls.key_password == "s3cret"
