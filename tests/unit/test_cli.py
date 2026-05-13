"""Tests for the uvicorn-config builder in i3xua.cli."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import Mock

import pytest

from i3xua.cli import _build_uvicorn_config
from i3xua.settings import ServerConfig, ServerTLS


def _cfg_with_tls(tls: ServerTLS | None) -> Mock:
    """Mock just enough of AppConfig for _build_uvicorn_config."""
    cfg = Mock()
    cfg.server = ServerConfig.model_validate({"tls": tls.model_dump(mode="json") if tls else None})
    cfg.logging.level = "INFO"
    return cfg


def test_build_uvicorn_config_warns_when_tls_absent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cfg = _cfg_with_tls(None)
    app = object()
    with caplog.at_level(logging.WARNING, logger="i3xua.cli"):
        uconfig = _build_uvicorn_config(cfg, app)

    assert any(
        "WITHOUT TLS" in record.getMessage() and record.levelno == logging.WARNING
        for record in caplog.records
    )
    # Sanity: no ssl kwargs leaked through.
    assert uconfig.ssl_certfile is None
    assert uconfig.ssl_keyfile is None


def test_build_uvicorn_config_passes_ssl_kwargs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cert = tmp_path / "server.crt"
    key = tmp_path / "server.key"
    cert.write_bytes(b"x")
    key.write_bytes(b"x")
    tls = ServerTLS.model_validate(
        {"cert_path": str(cert), "key_path": str(key), "key_password": "pw"}
    )
    cfg = _cfg_with_tls(tls)
    app = object()
    with caplog.at_level(logging.WARNING, logger="i3xua.cli"):
        uconfig = _build_uvicorn_config(cfg, app)

    assert uconfig.ssl_certfile == str(cert)
    assert uconfig.ssl_keyfile == str(key)
    assert uconfig.ssl_keyfile_password == "pw"
    # No "WITHOUT TLS" warning when TLS is configured.
    assert not any("WITHOUT TLS" in record.getMessage() for record in caplog.records)
