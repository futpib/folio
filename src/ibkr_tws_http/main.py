from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import uvicorn

from .app import create_app
from .config import ConfigError, ServerConfig, Settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ibkr-tws-http",
        description="Expose selected local IBKR TWS data over a read-only HTTP API.",
    )
    parser.add_argument("--config", type=Path, help="TOML configuration file")
    parser.add_argument("--host", help="HTTP bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, help="HTTP bind port (default: 8080)")
    parser.add_argument("--tws-port", type=int, help="TWS socket port (default: 7496)")
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate configuration and exit without connecting to TWS",
    )
    parser.add_argument(
        "--log-level",
        choices=("critical", "error", "warning", "info", "debug"),
        default="info",
    )
    return parser


def load_settings(args: argparse.Namespace) -> Settings:
    settings = Settings.load(args.config) if args.config else Settings(server=ServerConfig())
    server = replace(
        settings.server,
        host=args.host if args.host is not None else settings.server.host,
        port=args.port if args.port is not None else settings.server.port,
    )
    tws = replace(
        settings.tws,
        port=args.tws_port if args.tws_port is not None else settings.tws.port,
    )
    settings = replace(settings, server=server, tws=tws)
    settings.validate()
    return settings


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args)
    except (ConfigError, ValueError) as exc:
        parser.error(str(exc))
    if args.check:
        print("configuration is valid")
        return 0

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if settings.server.host not in {"127.0.0.1", "::1", "localhost"}:
        logging.getLogger(__name__).warning(
            "binding an unauthenticated HTTP API to non-loopback host %s",
            settings.server.host,
        )
    app = create_app(settings)
    uvicorn.run(
        app,
        host=settings.server.host,
        port=settings.server.port,
        log_level=args.log_level,
    )
    return 0
