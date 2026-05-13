"""CLI entrypoint: load config, start the upstream adapter, run uvicorn."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from pathlib import Path
from typing import Any

import structlog
import uvicorn

from i3xua import __version__
from i3xua.adapters.asyncua.upstream import AsyncuaUpstreamDataSource
from i3xua.api.app_factory import build_app
from i3xua.api.state import build_state
from i3xua.settings import AppConfig, load_config

log = logging.getLogger(__name__)


def _configure_logging(cfg: AppConfig) -> None:
    logging.basicConfig(level=cfg.logging.level)
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    if cfg.logging.format == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())
    structlog.configure(processors=processors)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="i3xua",
        description="OPC UA 1.04 -> i3X v1.0 wrapper.",
    )
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--version", action="version", version=f"i3xua {__version__}")
    return parser.parse_args(argv)


def _build_uvicorn_config(cfg: AppConfig, app: Any) -> uvicorn.Config:
    """Build a uvicorn.Config from cfg.

    Wires ``ssl_*`` kwargs when ``cfg.server.tls`` is set; otherwise logs a
    WARNING that the server is running plain HTTP.
    """
    ssl_kwargs: dict[str, Any] = {}
    if cfg.server.tls is not None:
        ssl_kwargs = {
            "ssl_certfile": str(cfg.server.tls.cert_path),
            "ssl_keyfile": str(cfg.server.tls.key_path),
            "ssl_keyfile_password": cfg.server.tls.key_password,
        }
    else:
        log.warning(
            "i3X server starting WITHOUT TLS — i3X RFC v1.0-Beta §5.3.3 "
            "requires encrypted transport in production. Set "
            "server.tls.{cert_path,key_path} or terminate TLS at a reverse "
            "proxy in front of this process."
        )

    return uvicorn.Config(
        app,
        host=cfg.server.host,
        port=cfg.server.port,
        log_level=cfg.logging.level.lower(),
        # Per-request access logs flood the console; startup and warnings remain on the main logger.
        access_log=False,
        lifespan="off",
        **ssl_kwargs,
    )


async def _run(cfg: AppConfig) -> None:
    upstream = AsyncuaUpstreamDataSource(cfg)
    state = build_state(cfg, upstream=upstream)
    upstream.bind_registries(
        namespaces=state.namespaces,
        types=state.types,
        instances=state.instances,
        subscriptions=state.subscriptions,
        history=state.history,
        # Per-connection browse timings written on connect; /admin/state surfaces them.
        browse_metrics=state.browse_metrics,
    )
    await upstream.start()
    app = build_app(state)

    server_cfg = _build_uvicorn_config(cfg, app)
    server = uvicorn.Server(server_cfg)

    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _sigterm(*_: object) -> None:
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _sigterm)
        # On platforms where add_signal_handler is unsupported (Windows), uvicorn's shutdown handler takes over.

    serve_task = asyncio.create_task(server.serve())
    await shutdown_event.wait()
    server.should_exit = True
    await serve_task
    await upstream.stop()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if not args.config.exists():
        print(f"config not found: {args.config}", file=sys.stderr)
        return 2
    cfg = load_config(args.config)
    _configure_logging(cfg)
    asyncio.run(_run(cfg))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
