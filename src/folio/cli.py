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
        uvicorn.run(
            create_app(settings),
            host="127.0.0.1",
            port=settings.port,
            access_log=False,
            timeout_graceful_shutdown=5,
        )
    except (OSError, ValueError) as error:
        parser.exit(1, f"foliod: {error}\n")


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
    call = sub.add_parser("call", help="make one live adapter call for a smoke test")
    call.add_argument("connection")
    call.add_argument("endpoint", help="provider path or URL (IBKR: local /v1/... route)")
    call.add_argument("--param", action="append", default=[], metavar="KEY=VALUE")
    call.add_argument(
        "--method", choices=["GET", "POST"], default="GET", help="POST is for IBKR read queries"
    )
    data = call.add_mutually_exclusive_group()
    data.add_argument("--data", help="JSON request body for IBKR POST queries")
    data.add_argument("--data-file", type=Path, help="file containing a JSON request body")
    call.add_argument(
        "--public", action="store_true", help="Binance: omit credentials and signature"
    )
    call.add_argument("--verbose", action="store_true", help="show request metadata on stderr")
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
        if args.command == "login":
            connection = settings.connection(args.connection)
            if connection.provider not in {"bog", "wolt"}:
                parser.error("login requires a browser connection; use 'folio sync NAME' for APIs")
            Store(settings.state_dir)
            asyncio.run(login(settings, connection))
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
            elif args.command == "call":
                from .calls import CallRequest

                connection = settings.connection(args.connection)
                params = []
                for value in args.param:
                    key, separator, value = value.partition("=")
                    if not separator or not key:
                        parser.error("--param requires KEY=VALUE")
                    params.append((key, value))
                spec = CallRequest(
                    endpoint=args.endpoint,
                    method=args.method,
                    params=params,
                    body=args.data_file.read_text() if args.data_file else args.data,
                    public=args.public,
                )
                response = client.post(
                    f"/v1/connections/{connection.id}/call",
                    json=spec.model_dump(),
                    timeout=httpx.Timeout(360, connect=15),
                )
                result = response.json()
                if args.verbose:
                    for row in result.get("logs", []):
                        print(format_log(row), file=sys.stderr)
                if "body" not in result:
                    print(json.dumps(result, indent=2, ensure_ascii=False), file=sys.stderr)
                    raise SystemExit(1)
                print(f"HTTP {result['status']}", file=sys.stderr)
                print(result["body"], end="" if result["body"].endswith("\n") else "\n")
                if not 200 <= result["status"] < 300:
                    raise SystemExit(1)
                return
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
        if args.command == "call":
            parser.exit(130, "Stopped the one-off call.\n")
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
