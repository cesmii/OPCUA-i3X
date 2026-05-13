"""YAML + env-variable config loader.

`load_config(path)` reads YAML, expands `${ENV_VAR}` references, and validates
against the Pydantic `AppConfig`. Invalid configs raise ValidationError at
startup so operators see the problem immediately.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_ENV_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    return value


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoneAuth(_Strict):
    mode: Literal["none"] = "none"


class BasicUser(_Strict):
    username: str
    password: str


class BasicAuth(_Strict):
    mode: Literal["basic"]
    users: list[BasicUser] = Field(min_length=1)


class BearerAuth(_Strict):
    mode: Literal["bearer"]
    tokens: list[str] = Field(min_length=1)

    @field_validator("tokens")
    @classmethod
    def _no_empty_tokens(cls, v: list[str]) -> list[str]:
        if any(not t for t in v):
            raise ValueError("bearer token list must not contain empty strings")
        return v


ServerAuth = Annotated[NoneAuth | BasicAuth | BearerAuth, Field(discriminator="mode")]


class ServerTLS(_Strict):
    """Native TLS for the i3X HTTP server.

    When set, uvicorn terminates TLS on `ServerConfig.port`. When absent,
    the server runs plain HTTP and emits a startup WARNING citing i3X RFC
    v1.0-Beta §5.3.3 (encrypted transport required in production).

    `key_password` flows through `_expand_env` like any other string field,
    so YAML can carry `${I3XUA_KEY_PASSWORD}` and the env lookup happens at
    config-load time.

    mTLS, HSTS, HTTP→HTTPS redirect, dual-port listening, and cipher
    pinning are out of scope.
    """

    cert_path: Path
    key_path: Path
    key_password: str | None = None

    @field_validator("cert_path", "key_path")
    @classmethod
    def _file_must_exist(cls, v: Path) -> Path:
        if not v.is_file():
            raise ValueError(f"file does not exist: {v}")
        return v


class ServerConfig(_Strict):
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)
    versions: list[Literal["v0", "v1"]] = Field(
        default_factory=lambda: ["v0", "v1"]  # type: ignore[arg-type,unused-ignore]
    )
    openapi_regen_on_admin_refresh: bool = True
    auth: ServerAuth = Field(default_factory=NoneAuth)
    tls: ServerTLS | None = None


class LoggingConfig(_Strict):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    format: Literal["json", "console"] = "json"


class RegistryConfig(_Strict):
    history_ring_size: int = Field(default=1000, ge=1)
    subscription_ring_size: int = Field(default=10000, ge=1)
    rebrowse_interval_seconds: int = Field(default=300, ge=0)


class ChannelNone(_Strict):
    mode: Literal["None"] = "None"
    policy: Literal["None"] = "None"


class ChannelSigned(_Strict):
    """Encrypted/signed SecureChannel.

    `server_trust_list_dir` is OPTIONAL. When set, the server certificate
    discovered during `GetEndpoints` is SHA-256-matched against the directory
    contents before the SecureChannel is opened — refuse-on-no-match. When
    omitted, the wrapper auto-trusts whatever cert the server returns during
    discovery (the cert is the identity; appropriate for trusted-network
    setups without a maintained trust list).
    """

    mode: Literal["Sign", "SignAndEncrypt"]
    policy: Literal["Basic256Sha256", "Aes128Sha256RsaOaep", "Aes256Sha256RsaPss"]
    client_cert_path: Path
    client_key_path: Path
    server_trust_list_dir: Path | None = None

    @field_validator("client_cert_path", "client_key_path")
    @classmethod
    def _file_must_exist(cls, v: Path) -> Path:
        if not v.is_file():
            raise ValueError(f"file does not exist: {v}")
        return v

    @field_validator("server_trust_list_dir")
    @classmethod
    def _dir_must_exist(cls, v: Path | None) -> Path | None:
        if v is None:
            return None
        if not v.is_dir():
            raise ValueError(f"directory does not exist: {v}")
        return v


ChannelSecurity = Annotated[
    ChannelNone | ChannelSigned,
    Field(discriminator="mode"),
]


class AnonymousUser(_Strict):
    type: Literal["anonymous"] = "anonymous"


class UsernameUser(_Strict):
    type: Literal["username"]
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class X509User(_Strict):
    type: Literal["x509"]
    cert_path: Path
    key_path: Path

    @field_validator("cert_path", "key_path")
    @classmethod
    def _file_must_exist(cls, v: Path) -> Path:
        if not v.is_file():
            raise ValueError(f"file does not exist: {v}")
        return v


UserIdentity = Annotated[
    AnonymousUser | UsernameUser | X509User,
    Field(discriminator="type"),
]


class ReconnectConfig(_Strict):
    """per-connection backoff sequence (ms). Flat 5s retry by default."""

    backoff_ms: list[int] = Field(
        default_factory=lambda: [5000],
        min_length=1,
    )


class ConnectionConfig(_Strict):
    """subscription tiers are AUTO-CREATED at runtime keyed by the
    PublishingInterval requested in `POST /subscriptions/register`.
    `default_publishing_interval_ms` + `default_sampling_interval_ms` are used
    when the register body omits intervals.
    """

    name: str = Field(min_length=1, pattern=r"^[^!]+$")
    endpoint: str = Field(min_length=1)
    channel: ChannelSecurity = Field(default_factory=ChannelNone)
    user: UserIdentity = Field(default_factory=AnonymousUser)
    namespace_allowlist: list[str] = Field(default_factory=list)
    default_publishing_interval_ms: int = Field(default=1000, ge=50)
    default_sampling_interval_ms: int = Field(default=500, ge=50)
    reconnect: ReconnectConfig = Field(default_factory=ReconnectConfig)
    # See BrowseConfig.browse_variable_properties. Tag-heavy SCADA servers
    # store Variable metadata as node ATTRIBUTES only; HasProperty descent
    # finds nothing. Default False matches that shape. Set True for OPC UA
    # servers that publish HasProperty children (Alarms/Conditions, etc.).
    browse_variable_properties: bool = Field(default=False)
    # Hard cap on parents per BrowseRequest. The server's
    # OperationLimits.MaxNodesPerBrowse may clamp this further at connect.
    # The default 1000 is permissive for most servers; the children-budget
    # is the primary backpressure for adaptive batching.
    max_parents_per_batch: int = Field(default=1_000, ge=1)
    # Soft target for total children returned across one batch. The walker's
    # running-mean adaptive sizer packs batches to approach this number.
    # Lower if a server reports Failed-to-send under load; raise for servers
    # with looser response-size budgets.
    max_children_per_batch: int = Field(default=50_000, ge=1)

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_thread(cls, data: Any) -> Any:
        if isinstance(data, dict) and "thread" in data:
            raise ValueError(
                "Configuration error: 'connections[*].thread' was removed in this release. "
                "The wrapper now creates one worker thread per connection automatically. "
                "Delete the 'thread:' field from each connection."
            )
        return data

    @model_validator(mode="after")
    def _no_username_over_cleartext(self) -> ConnectionConfig:
        if isinstance(self.channel, ChannelNone) and isinstance(self.user, UsernameUser):
            raise ValueError(
                "cleartext channel (channel.mode=None) is not allowed with "
                "user.type=username; use anonymous or pair with an encrypted channel"
            )
        return self

    @field_validator("endpoint")
    @classmethod
    def _opc_tcp(cls, v: str) -> str:
        if not (v.startswith("opc.tcp://") or v.startswith("opc.wss://")):
            raise ValueError("endpoint must start with opc.tcp:// or opc.wss://")
        return v


class AppConfig(_Strict):
    server: ServerConfig = Field(default_factory=ServerConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    registry: RegistryConfig = Field(default_factory=RegistryConfig)
    connections: list[ConnectionConfig] = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_threads(cls, data: Any) -> Any:
        if isinstance(data, dict) and "threads" in data:
            raise ValueError(
                "Configuration error: 'threads:' was removed in this release. "
                "The wrapper now creates one worker thread per connection automatically. "
                "Delete the 'threads:' block from your config."
            )
        return data

    @model_validator(mode="after")
    def _unique_connection_names(self) -> AppConfig:
        names = [c.name for c in self.connections]
        if len(set(names)) != len(names):
            raise ValueError("connection names must be unique")
        return self


def load_config(path: str | Path) -> AppConfig:
    raw = yaml.safe_load(Path(path).read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"config root must be a mapping, got {type(raw).__name__}")
    return AppConfig.model_validate(_expand_env(raw))
