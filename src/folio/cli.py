from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx
import uvicorn

from .browser import BrowserConnector, login
from .config import Settings, default_config
from .store import Store


def common(parser):
    parser.add_argument("--config", type=Path, default=default_config())


def daemon():
    from .service import create_app

    parser = argparse.ArgumentParser(description="Folio read-only local data service")
    common(parser)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        settings = Settings.load(args.config)
        if args.check:
            print("Folio configuration is valid")
            return
        os.umask(0o077)
        uvicorn.run(create_app(settings), host="127.0.0.1", port=settings.port, access_log=False)
    except (OSError, ValueError) as error:
        parser.exit(1, f"foliod: {error}\n")


async def adapter(settings, connection):
    from .broker import Broker
    from .service import worker

    broker = Broker()
    await broker.start()
    try:
        store = Store(settings.state_dir)
        store.register(connection)
        await worker(store, connection, BrowserConnector(settings, connection, broker), broker)
    finally:
        broker.close()


def main():
    parser = argparse.ArgumentParser(description="Read personal account data through Folio")
    common(parser)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("connections", help="list connections, capabilities and freshness")
    read = sub.add_parser("read", help="read collected data without contacting the provider")
    read.add_argument("connection")
    read.add_argument("resource")
    read.add_argument("--since")
    read.add_argument("--limit", type=int, default=100)
    read.add_argument("--offset", type=int, default=0)
    sync = sub.add_parser("sync", help="queue a sync and follow it until completion")
    sync.add_argument("connection")
    sync.add_argument("--detach", action="store_true")
    job = sub.add_parser("job", help="inspect or follow a durable sync job")
    job.add_argument("id")
    job.add_argument("--follow", action="store_true")
    signin = sub.add_parser("login", help="sign in using a dedicated visible Chromium profile")
    signin.add_argument("connection")
    broker = sub.add_parser("browser-adapter", help="run as a zond credential adapter")
    broker.add_argument("connection")
    args = parser.parse_args()
    try:
        settings = Settings.load(args.config)
        os.umask(0o077)
        if args.command in {"login", "browser-adapter"}:
            connection = settings.connection(args.connection)
            if connection.provider == "ibkr":
                parser.error("IBKR uses the existing Gateway credential adapter")
            Store(settings.state_dir)
            if args.command == "login":
                asyncio.run(login(settings, connection))
            else:
                if os.environ.get("ZOND_ADAPTER_PROTOCOL") != "1":
                    parser.error("browser-adapter must be launched by zond")
                if not connection.credential_adapter:
                    parser.error("set credential_adapter = true for this connection first")
                asyncio.run(adapter(settings, connection))
            return
        with httpx.Client(
            base_url=f"http://127.0.0.1:{settings.port}",
            trust_env=False,
            timeout=15,
            follow_redirects=False,
        ) as client:
            if args.command == "connections":
                response = client.get("/v1/connections")
            elif args.command == "read":
                connection = settings.connection(args.connection)
                if args.resource not in connection.resources:
                    parser.error("unsupported resource; see 'folio connections'")
                response = client.get(
                    f"/v1/connections/{connection.id}/records/{args.resource}",
                    params={
                        k: v
                        for k, v in {
                            "since": args.since,
                            "limit": args.limit,
                            "offset": args.offset,
                        }.items()
                        if v is not None
                    },
                )
            elif args.command == "sync":
                connection = settings.connection(args.connection)
                response = client.post(f"/v1/connections/{connection.id}/sync", json={})
            else:
                # Never interpolate arbitrary path segments into a request.
                from uuid import UUID

                response = client.get(f"/v1/jobs/{UUID(args.id)}")
            response.raise_for_status()
            result = response.json()
            follow = (args.command == "sync" and not args.detach) or (
                args.command == "job" and args.follow
            )
            if follow:
                print(
                    f"Following job {result['id']}; Ctrl-C detaches without cancelling it.",
                    file=sys.stderr,
                )
                while result["state"] in {"queued", "running"}:
                    time.sleep(1)
                    response = client.get(f"/v1/jobs/{result['id']}")
                    response.raise_for_status()
                    result = response.json()
            print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
            if isinstance(result, dict) and result.get("state") == "failed":
                raise SystemExit(1)
    except KeyboardInterrupt:
        parser.exit(130, "Detached. Use 'folio job ID --follow' to resume following.\n")
    except (OSError, ValueError, httpx.HTTPError) as error:
        # No provider response bodies, credentials or browser exceptions in CLI diagnostics.
        parser.exit(1, f"folio: {error}\n")
