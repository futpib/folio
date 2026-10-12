from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import uvicorn

from .browser import login
from .config import Settings, default_config
from .credentials import CredentialFileError
from .observability import format_log
from .store import Store


def common(parser):
    parser.add_argument("--config", type=Path, default=default_config())
    parser.add_argument("--watch-config", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--worker-revision", help=argparse.SUPPRESS)


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


async def adapter(settings, connection, *, watch_config=None):
    from .broker import credential_relay

    if watch_config is None:
        await credential_relay(settings, connection)
    else:
        original = watch_config.read_bytes()

        async def changed():
            while True:
                await asyncio.sleep(0.5)
                if watch_config.read_bytes() != original:
                    return

        tasks = [
            asyncio.create_task(credential_relay(settings, connection)),
            asyncio.create_task(changed()),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def main():
    from .configure import add_commands

    parser = argparse.ArgumentParser(description="Read personal account data through Folio")
    common(parser)
    sub = parser.add_subparsers(dest="command", required=True)
    add_commands(sub)
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
    sync.add_argument("--verbose", action="store_true", help="show each provider request on stderr")
    job = sub.add_parser("job", help="inspect or follow a durable sync job")
    job.add_argument("id")
    job.add_argument("--follow", action="store_true")
    job.add_argument("--verbose", action="store_true", help="show the job's provider request log")
    logs = sub.add_parser("logs", help="view retained provider request logs")
    logs.add_argument("connection")
    logs.add_argument("--job", help="filter to a job ID")
    logs.add_argument("--follow", action="store_true")
    signin = sub.add_parser("login", help="sign in using a dedicated visible Chromium profile")
    signin.add_argument("connection")
    broker = sub.add_parser("browser-adapter", help="compatibility alias for credential-relay")
    broker.add_argument("connection")
    api = sub.add_parser("api-adapter", help="Binance compatibility alias for credential-relay")
    api.add_argument("connection")
    relay = sub.add_parser(
        "credential-relay", help="connect Zond authentication to standalone Folio"
    )
    relay.add_argument("connection")
    keygen = sub.add_parser(
        "binance-keygen", help="store a signing key locally and print its public key"
    )
    keygen.add_argument("--type", choices=["ed25519", "rsa"], default="ed25519")
    keygen.add_argument(
        "--env-file", type=Path, help="credentials file (default: checkout .env.local)"
    )
    args = parser.parse_args()
    try:
        os.umask(0o077)
        if args.command in {"setup", "config", "credentials"}:
            from .configure import run

            run(args)
            return
        if args.command == "binance-keygen":
            from .binance_keys import generate_key

            print(generate_key(args.type, env_file=args.env_file), end="")
            return
        settings = Settings.load(args.config)
        if args.command in {"login", "browser-adapter", "api-adapter", "credential-relay"}:
            connection = settings.connection(args.connection)
            if connection.provider == "ibkr":
                parser.error("IBKR uses the existing Gateway credential adapter")
            if connection.provider == "binance" and args.command == "login":
                parser.error(
                    "Binance uses local API keys with optional Zond fallback; run 'folio sync NAME'"
                )
            if args.command == "api-adapter" and connection.provider != "binance":
                parser.error("api-adapter requires a Binance connection")
            Store(settings.state_dir)
            if args.command == "login":
                asyncio.run(login(settings, connection))
            else:
                if os.environ.get("ZOND_ADAPTER_PROTOCOL") != "1":
                    parser.error("credential adapters must be launched by zond")
                if not connection.credential_adapter:
                    parser.error("set credential_adapter = true for this connection first")
                asyncio.run(
                    adapter(
                        settings,
                        connection,
                        watch_config=args.config.expanduser().resolve()
                        if args.watch_config
                        else None,
                    )
                )
            return
        with httpx.Client(
            base_url=f"http://127.0.0.1:{settings.port}",
            trust_env=False,
            timeout=15,
            follow_redirects=False,
        ) as client:
            if args.command == "logs":
                connection = settings.connection(args.connection)
                after = 0
                while True:
                    after = show_logs(client, connection.id, after, job=args.job)
                    if not args.follow:
                        return
                    time.sleep(1)
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
            after = 0
            if args.command in {"sync", "job"} and args.verbose:
                after = show_logs(
                    client, result["connection"], after, job=result["id"], stream=sys.stderr
                )
            if follow:
                while result["state"] in {"queued", "running"}:
                    time.sleep(1)
                    response = client.get(f"/v1/jobs/{result['id']}")
                    response.raise_for_status()
                    result = response.json()
                    if args.verbose:
                        after = show_logs(
                            client, result["connection"], after, job=result["id"], stream=sys.stderr
                        )
            print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
            if isinstance(result, dict) and result.get("state") == "failed":
                raise SystemExit(1)
    except KeyboardInterrupt:
        if args.command == "logs":
            parser.exit(130, "Stopped following logs.\n")
        if args.command in {"setup", "config", "credentials", "binance-keygen"}:
            parser.exit(130, "Cancelled.\n")
        parser.exit(130, "Detached. Use 'folio job ID --follow' to resume following.\n")
    except httpx.ConnectError:
        parser.exit(
            1, "Cannot reach Folio. Run 'folio setup' to configure and start its service.\n"
        )
    except (
        OSError,
        ValueError,
        EOFError,
        subprocess.SubprocessError,
        httpx.HTTPError,
        CredentialFileError,
    ) as error:
        # No provider response bodies, credentials or browser exceptions in CLI diagnostics.
        parser.exit(1, f"folio: {error}\n")


def show_logs(client, connection, after, *, job=None, stream=None):
    stream = sys.stdout if stream is None else stream
    while True:
        response = client.get(
            f"/v1/connections/{connection}/logs",
            params={"after": after, "limit": 1000, **({"job": job} if job is not None else {})},
        )
        response.raise_for_status()
        payload = response.json()
        for row in payload["items"]:
            print(format_log(row), file=stream, flush=True)
        after = payload["next_after"]
        if len(payload["items"]) < 1000:
            return after
