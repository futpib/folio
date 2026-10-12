from __future__ import annotations

import asyncio
import json
import os
import socket
import struct
import sys
import time
from contextlib import suppress
from hashlib import sha256
from pathlib import Path

from .models import LoginRequired
from .observability import emit
from .store import exclusive, private_directory


class Broker:
    """Private zond adapter protocol. Answers must never enter logs or persistent state."""

    async def start(self, reader=None, writer=None):
        self.writer = writer
        if reader is None:
            self.reader = asyncio.StreamReader(limit=128 * 1024)
            protocol = asyncio.StreamReaderProtocol(self.reader)
            self.transport, _ = await asyncio.get_running_loop().connect_read_pipe(
                lambda: protocol, sys.stdin
            )
        else:
            self.reader = reader
            self.transport = writer
        ready = json.loads(await self.reader.readline())
        if ready.get("type") != "ready" or ready.get("version") != 1:
            raise RuntimeError("unsupported zond adapter protocol")
        self.ready = ready
        self.replies = asyncio.Queue()
        self.retry = asyncio.Event()
        self.closed = False
        self.pump_task = asyncio.create_task(self.pump())

    def close(self):
        self.pump_task.cancel()
        self.transport.close()
        while not self.replies.empty():
            reply = self.replies.get_nowait()
            values = reply.pop("values", None)
            if values:
                values.clear()

    async def pump(self):
        try:
            while line := await self.reader.readline():
                message = json.loads(line)
                if message.get("type") == "retry":
                    self.retry.set()
                else:
                    await self.replies.put(message)
        finally:
            self.closed = True
            await self.replies.put({"type": "error"})

    async def receive(self):
        message = await self.replies.get()
        if message.get("type") == "error":
            raise RuntimeError("zond adapter channel closed or request rejected")
        return message

    def send(self, method, **body):
        frame = json.dumps({"method": method, **body})
        if self.writer is None:
            print(frame, flush=True)
        else:
            self.writer.write((frame + "\n").encode())

    async def status(self, state):
        self.send("status", state=state)
        await self.recorded()

    async def recorded(self):
        if (await self.receive()).get("type") != "recorded":
            raise RuntimeError("unexpected zond response")

    async def challenge(self, provider, fields, page_origin):
        emit("credentials_requested", source="zond")
        self.send(
            "challenge",
            prompt=f"Sign in to {provider} for Folio read access.",
            fields=fields,
            context={
                "provider": provider,
                "origin": page_origin,
                "purpose": "Read account history",
            },
            timeout_secs=300,
        )
        opened = await self.receive()
        if opened.get("type") != "opened":
            raise RuntimeError("expected zond challenge")
        return opened["challenge_id"]

    async def result(self, ident, outcome):
        emit("credentials_completed", outcome=outcome)
        self.send("result", challenge_id=ident, outcome=outcome)
        await self.recorded()

    async def withdraw(self, ident):
        self.send("withdraw", challenge_id=ident)
        # An answer may already be in flight. Discard its values on withdrawal.
        recorded = False
        answered = False
        while not (recorded and answered):
            reply = await self.receive()
            recorded |= reply.get("type") == "recorded"
            answered |= reply.get("type") == "answered" and reply.get("challenge_id") == ident
            values = reply.pop("values", None)
            if values:
                values.clear()


def socket_path(settings, connection):
    root = Path(os.environ.get("XDG_RUNTIME_DIR", f"/tmp/folio-{os.getuid()}")) / "folio-auth"
    ident = sha256(f"{settings.state_dir.resolve()}:{connection.id}".encode()).hexdigest()[:24]
    return root / f"{ident}.sock"


class ServiceBroker:
    """Connect to Zond only when the standalone service needs a missing field."""

    def __init__(self, settings, connection):
        self.path = socket_path(settings, connection)
        self.provider = connection.provider
        self.channel = None

    async def challenge(self, provider, fields, page_origin):
        if self.channel is None or self.channel.closed:
            deadline = time.monotonic() + 6
            while True:
                try:
                    reader, writer = await asyncio.open_unix_connection(self.path, limit=128 * 1024)
                    break
                except (FileNotFoundError, ConnectionRefusedError):
                    if time.monotonic() >= deadline:
                        raise LoginRequired(
                            "Required credentials are missing from .env.local and Zond's "
                            "credential relay is unavailable. Configure Zond, run "
                            f"'folio setup --yes', or use 'folio credentials {self.provider}'."
                        ) from None
                    await asyncio.sleep(0.1)
            self.channel = Broker()
            try:
                await asyncio.wait_for(self.channel.start(reader, writer), 5)
            except asyncio.CancelledError:
                writer.close()
                self.channel = None
                raise
            except (OSError, ValueError, RuntimeError, TimeoutError):
                writer.close()
                self.channel = None
                raise LoginRequired(
                    "Could not connect to the configured Zond credential relay."
                ) from None
        try:
            return await self.channel.challenge(provider, fields, page_origin)
        except RuntimeError:
            raise LoginRequired(
                "Zond disconnected during authentication; retry the sync."
            ) from None

    async def receive(self):
        try:
            return await self.channel.receive()
        except RuntimeError:
            raise LoginRequired(
                "Zond disconnected during authentication; retry the sync."
            ) from None

    async def result(self, ident, outcome):
        with suppress(RuntimeError, OSError):
            await self.channel.result(ident, outcome)

    async def withdraw(self, ident):
        with suppress(RuntimeError, OSError):
            await self.channel.withdraw(ident)

    def close(self):
        if self.channel is not None:
            self.channel.close()
            self.channel = None


async def credential_relay(settings, connection):
    """Private credential pipes only. Provider workers run inside foliod."""
    path = socket_path(settings, connection)
    private_directory(path.parent)
    broker = Broker()
    await broker.start()
    await broker.status("ready")
    finished = asyncio.Event()
    connected = False

    async def accept(reader, writer):
        nonlocal connected
        accepted = False
        try:
            peer_pid, uid, _ = struct.unpack(
                "3i",
                writer.get_extra_info("socket").getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED, 12
                ),
            )
            daemon_pid = int((settings.state_dir / "daemon.pid").read_text())
            if connected or uid != os.getuid() or peer_pid != daemon_pid:
                return
            connected = True
            accepted = True
            writer.write((json.dumps(broker.ready) + "\n").encode())
            await writer.drain()

            async def requests():
                while line := await reader.readline():
                    if len(line) > 65536:
                        raise ValueError("credential frame exceeds limit")
                    message = json.loads(line)
                    method = message.pop("method", None)
                    if method not in {"challenge", "result", "withdraw"}:
                        raise ValueError("unsupported credential request")
                    if method == "challenge":
                        from .credentials import FIELDS

                        allowed = set(FIELDS[connection.provider]) | {"otp"}
                        fields = message.get("fields", [])
                        if not fields or any(field.get("id") not in allowed for field in fields):
                            raise ValueError("unexpected credential field")
                        if connection.provider == "binance":
                            from .binance import ORIGINS as sites

                            allowed_origins = set(sites.values())
                            expected_provider = "Binance API"
                        else:
                            from .providers import LOGIN_ORIGINS

                            allowed_origins = LOGIN_ORIGINS[connection.provider]
                            expected_provider = connection.provider
                        context = message.get("context", {})
                        if (
                            context.get("provider") != expected_provider
                            or not set(context.get("origin", "").split(", ")) <= allowed_origins
                        ):
                            raise ValueError("unexpected credential origin")
                    broker.send(method, **message)
                raise EOFError

            async def answers():
                while True:
                    reply = await broker.receive()
                    writer.write((json.dumps(reply) + "\n").encode())
                    await writer.drain()
                    values = reply.pop("values", None)
                    if values:
                        values.clear()

            tasks = [asyncio.create_task(requests()), asyncio.create_task(answers())]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        except (OSError, ValueError, RuntimeError, EOFError, TypeError, KeyError):
            pass
        finally:
            writer.close()
            if accepted:
                finished.set()
            with suppress(OSError):
                await writer.wait_closed()

    owns_socket = False
    try:
        with exclusive(path.with_suffix(".lock")):
            if path.exists() or path.is_symlink():
                if path.is_symlink() or not path.is_socket() or path.stat().st_uid != os.getuid():
                    raise ValueError("refused unsafe credential socket")
                path.unlink()
            server = await asyncio.start_unix_server(accept, path, limit=65537)
            owns_socket = True
            path.chmod(0o600)
            async with server:
                while not finished.is_set() and not broker.closed:
                    with suppress(TimeoutError):
                        await asyncio.wait_for(finished.wait(), 0.25)
    finally:
        if owns_socket:
            path.unlink(missing_ok=True)
        broker.close()
