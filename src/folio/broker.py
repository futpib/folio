from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import socket
import stat
import struct
from contextlib import suppress
from hashlib import sha256
from pathlib import Path

from .models import LoginRequired
from .observability import emit
from .store import private_directory

ERROR_CODES = {
    "authentication_failed",
    "unsupported_version",
    "busy",
    "origin_not_allowed",
    "field_not_allowed",
    "invalid_challenge",
    "result_required",
    "unknown_challenge",
    "answer_delivered",
    "invalid_outcome",
    "already_authenticated",
    "invalid_frame",
    "session_closed",
    "queue_full",
    "unavailable",
}


class CredentialAPIError(LoginRequired):
    def __init__(self, message, reason=None):
        super().__init__(message)
        self.reason = reason


def default_socket():
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        root = Path(runtime) / "zondd"
    elif Path(f"/run/user/{os.getuid()}").is_dir():
        root = Path(f"/run/user/{os.getuid()}/zondd")
    else:
        root = Path(f"/tmp/zondd-{os.getuid()}")
    return root / "credential.sock"


def identity_path(settings, connection):
    return settings.state_dir / "zond" / f"{connection.id}.key"


def recipient_name(ident):
    suffix = (
        ident if len(ident) <= 58 else ident[:45] + "-" + sha256(ident.encode()).hexdigest()[:12]
    )
    return "folio-" + suffix


def trusted_path(path):
    if not path.is_absolute():
        raise ValueError("credential paths must be absolute")
    for ancestor in (path, *path.parents):
        meta = ancestor.lstat()
        sticky = (
            ancestor != path
            and stat.S_ISDIR(meta.st_mode)
            and meta.st_uid == 0
            and meta.st_mode & stat.S_ISVTX
        )
        if (
            stat.S_ISLNK(meta.st_mode)
            or meta.st_uid not in {0, os.getuid()}
            or (meta.st_mode & 0o022 and not sticky)
        ):
            raise ValueError("credential paths must have trusted owners and permissions")


def read_identity(path):
    trusted_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        meta = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(meta.st_mode)
            or meta.st_uid != os.getuid()
            or meta.st_mode & 0o077
            or meta.st_size > 128
        ):
            raise ValueError("Zond identity must be a private, bounded file owned by this user")
        key = stream.read(129).decode("ascii").strip()
    if not re.fullmatch(r"[a-f0-9]{64}", key):
        raise ValueError("invalid Zond identity file")
    return key


def provision_identity(settings, connection):
    path = identity_path(settings, connection)
    parent = path.parent
    existing = parent
    while not existing.exists():
        if existing.is_symlink():
            raise ValueError("unsafe Zond identity directory")
        existing = existing.parent
    trusted_path(existing)
    private_directory(parent)
    trusted_path(parent)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w") as stream:
            stream.write(secrets.token_hex(32) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    return read_identity(path)


class ServiceBroker:
    """Lazy client of Zond's public credential API v1; provider jobs belong to Folio."""

    def __init__(self, settings, connection):
        self.path = settings.zond_socket or default_socket()
        self.key_path = identity_path(settings, connection)
        self.recipient = recipient_name(connection.id)
        self.provider = connection.provider
        self.writer = None
        self.pump_task = None
        self.replies = asyncio.Queue(maxsize=8)
        self.closed = True
        self.next_id = 0
        self.pending = None

    def failure(self, code=None):
        reason = code if isinstance(code, str) and code in ERROR_CODES else None
        detail = f" ({reason})" if reason else ""
        return CredentialAPIError(
            f"Zond credential API unavailable or rejected the request{detail}. "
            "Local credentials are missing; run 'folio setup --yes' to register Folio, "
            f"or 'folio credentials {self.provider}', then retry the sync.",
            reason=reason,
        )

    async def connect(self):
        if not self.closed:
            return
        self.close()
        self.replies = asyncio.Queue(maxsize=8)
        try:
            key = read_identity(self.key_path)
            trusted_path(self.path)
            metadata = self.path.lstat()
            if (
                not stat.S_ISSOCK(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o077
            ):
                raise ValueError("Zond credential endpoint must be an owner-only socket")
            reader, self.writer = await asyncio.wait_for(
                asyncio.open_unix_connection(self.path, limit=128 * 1024), 5
            )
            _, uid, _ = struct.unpack(
                "3i",
                self.writer.get_extra_info("socket").getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED, 12
                ),
            )
            if uid != os.getuid():
                raise ValueError("unexpected credential broker owner")
            self.closed = False
            request = await self.send("authenticate", recipient=self.recipient, key=key)
            key = None
            reply = await asyncio.wait_for(self.read_frame(reader), 5)
            if reply["id"] != request or reply["body"].get("type") != "ready":
                raise self.failure(reply["body"].get("code"))
            self.pump_task = asyncio.create_task(self.pump(reader, self.replies))
        except asyncio.CancelledError:
            self.close()
            raise
        except LoginRequired:
            self.close()
            raise
        except (OSError, ValueError, RuntimeError, TimeoutError):
            self.close()
            raise self.failure() from None

    async def send(self, method, **body):
        self.next_id += 1
        frame = (
            json.dumps({"version": 1, "id": self.next_id, "body": {"method": method, **body}})
            + "\n"
        ).encode()
        try:
            if len(frame) > 64 * 1024 or self.closed or self.writer is None:
                raise ValueError("invalid credential request")
            self.writer.write(frame)
            await asyncio.wait_for(self.writer.drain(), 5)
            return self.next_id
        except asyncio.CancelledError:
            self.close()
            raise
        except (OSError, ValueError, TimeoutError):
            self.close()
            raise self.failure() from None

    async def read_frame(self, reader):
        line = await reader.readline()
        if not line.endswith(b"\n") or len(line) > 128 * 1024:
            raise ValueError("credential channel closed or frame too large")
        message = json.loads(line)
        if (
            not isinstance(message, dict)
            or set(message) != {"version", "id", "body"}
            or type(message["version"]) is not int
            or message["version"] != 1
            or type(message["id"]) is not int
            or not 1 <= message["id"] <= self.next_id
            or not isinstance(message["body"], dict)
        ):
            raise ValueError("invalid credential envelope")
        body = message["body"]
        shapes = {
            "ready": {"type", "session_id"},
            "opened": {"type", "challenge_id"},
            "answered": {"type", "challenge_id", "status", "values"},
            "recorded": {"type", "challenge_id"},
            "error": {"type", "code"},
        }
        kind = body.get("type")
        if not isinstance(kind, str) or set(body) != shapes.get(kind):
            raise ValueError("invalid credential event")
        for key in body.keys() - {"values"}:
            if not isinstance(body[key], str) or not body[key]:
                raise ValueError("invalid credential metadata")
        if kind == "answered":
            values = body["values"]
            if (
                body["status"] not in {"answered", "rejected", "expired", "withdrawn"}
                or (body["status"] != "answered" and values is not None)
                or (
                    values is not None
                    and (
                        not isinstance(values, dict)
                        or not all(isinstance(value, str) for value in values.values())
                    )
                )
            ):
                raise ValueError("invalid credential answer")
        return message

    async def pump(self, reader, replies):
        try:
            while True:
                await replies.put(await self.read_frame(reader))
        except (OSError, ValueError):
            pass
        finally:
            if self.replies is replies:
                self.closed = True
            with suppress(asyncio.QueueFull):
                replies.put_nowait(None)

    async def receive_frame(self):
        if self.closed and self.replies.empty():
            raise self.failure()
        frame = await self.replies.get()
        if frame is None:
            raise self.failure()
        return frame

    async def expect(self, request, kind, ident=None):
        frame = await self.receive_frame()
        body = frame["body"]
        if frame["id"] == request and body.get("type") == "error":
            raise self.failure(body.get("code"))
        if (
            frame["id"] != request
            or body.get("type") != kind
            or (ident is not None and body.get("challenge_id") != ident)
        ):
            self.close()
            raise self.failure()
        return body

    async def challenge(self, provider, fields, page_origin):
        await self.connect()
        emit("credentials_requested", source="zond")
        request = await self.send(
            "challenge",
            origin=page_origin,
            prompt=f"Sign in to {provider} for Folio read access.",
            fields=fields,
            context={"provider": provider, "purpose": "Read account history"},
            timeout_secs=300,
        )
        try:
            opened = await asyncio.wait_for(self.expect(request, "opened"), 5)
        except TimeoutError:
            self.close()
            raise self.failure() from None
        ident = opened.get("challenge_id")
        if not isinstance(ident, str) or not ident:
            self.close()
            raise self.failure()
        self.pending = request, ident
        return ident

    async def receive(self):
        request, ident = self.pending
        return await self.expect(request, "answered", ident)

    async def result(self, ident, outcome):
        emit("credentials_completed", outcome=outcome)
        try:
            request = await self.send("result", challenge_id=ident, outcome=outcome)
            await asyncio.wait_for(self.expect(request, "recorded", ident), 5)
        except (LoginRequired, TimeoutError):
            self.close()
        finally:
            self.pending = None

    async def withdraw(self, ident):
        async def discard():
            challenge_request, _ = self.pending
            request = await self.send("withdraw", challenge_id=ident)
            terminal = acknowledged = delivered = False
            while not (terminal and acknowledged):
                frame = await self.receive_frame()
                body = frame["body"]
                if (
                    frame["id"] == challenge_request
                    and body.get("type") == "answered"
                    and body.get("challenge_id") == ident
                ):
                    values = body.pop("values", None)
                    delivered = values is not None
                    if isinstance(values, dict):
                        values.clear()
                    terminal = True
                elif frame["id"] == request and (
                    (body.get("type") == "recorded" and body.get("challenge_id") == ident)
                    or (
                        body.get("type") == "error"
                        and body.get("code") in {"answer_delivered", "unknown_challenge"}
                    )
                ):
                    acknowledged = True
                else:
                    raise self.failure()
            if delivered:
                await self.result(ident, "cancelled")

        try:
            await asyncio.wait_for(discard(), 5)
        except (LoginRequired, TimeoutError):
            self.close()
        finally:
            self.pending = None

    def close(self):
        if self.pump_task is not None:
            self.pump_task.cancel()
            self.pump_task = None
        if self.writer is not None:
            self.writer.close()
            self.writer = None
        self.closed = True
        self.next_id = 0
        self.pending = None
        while not self.replies.empty():
            frame = self.replies.get_nowait()
            if isinstance(frame, dict):
                values = frame.get("body", {}).pop("values", None)
                if isinstance(values, dict):
                    values.clear()
