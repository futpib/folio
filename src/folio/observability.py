"""Request metadata only: never record queries, headers, bodies or exception text."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import time
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from datetime import UTC, datetime
from urllib.parse import urlsplit

from .providers import LOGIN_ORIGINS, ORIGINS, PAGES, WOLT_ORDERS, origin

current = ContextVar("folio_request_log", default=None)


class Recorder:
    def __init__(self, store, connection, job=None, *, call_id=None):
        self.store, self.connection, self.job = store, connection, job
        self.call_id, self.items = call_id, []

    def emit(self, event, **fields):
        if self.call_id is not None:
            fields["call_id"] = self.call_id
        try:
            row = self.store.append_log(self.connection, self.job, event, fields)
        except (OSError, sqlite3.Error):
            row = {
                "connection": self.connection.id,
                "provider": self.connection.provider,
                "job": self.job,
                "created": time.time(),
                "event": event,
                "fields": {**fields, "log_persistence": "failed"},
            }
        with suppress(OSError, ValueError):
            print(json.dumps(row, separators=(",", ":")), file=sys.stderr, flush=True)
        if self.call_id is not None:
            self.items.append(row)


@contextmanager
def recording(store, connection, job=None, *, call_id=None):
    recorder = Recorder(store, connection, job, call_id=call_id)
    token = current.set(recorder)
    try:
        yield recorder
    finally:
        current.reset(token)


def emit(event, **fields):
    if recorder := current.get():
        recorder.emit(event, **fields)


class RequestLog:
    def __init__(self, method, endpoint, recorder=None):
        self.recorder = recorder or current.get()
        # Endpoints must come from fixed routes/templates, never provider-supplied URLs.
        self.fields = {
            "method": method
            if method in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
            else "OTHER",
            "endpoint": endpoint,
        }
        self.started = time.monotonic()
        self.finished = False
        if self.recorder:
            self.recorder.emit("request_started", **self.fields)

    def finish(self, *, status=None, code=None, error_code=None):
        if self.finished:
            return
        self.finished = True
        fields = {**self.fields, "duration_ms": round((time.monotonic() - self.started) * 1000)}
        if type(status) is int and 100 <= status <= 599:
            fields["status"] = status
        if type(code) is int and -999999 <= code <= 999999:
            fields["code"] = code
        if error_code is not None:
            fields["error_code"] = error_code
        if self.recorder:
            self.recorder.emit("request_finished", **fields)

    def __enter__(self):
        return self

    def __exit__(self, kind, error, traceback):
        if not self.finished:
            code = "cancelled" if kind and issubclass(kind, asyncio.CancelledError) else "failed"
            self.finish(error_code=code if kind else None)


def browser_endpoint(provider, request):
    if request.resource_type not in {"document", "xhr", "fetch"}:
        return None
    site = origin(request.url)
    if site not in ORIGINS[provider] | LOGIN_ORIGINS[provider]:
        return None
    path = urlsplit(request.url).path
    known = {urlsplit(url).path for url in PAGES[provider]}
    known |= {"/rest/rb", "/rest/transactional/statements"} if provider == "bog" else {WOLT_ORDERS}
    # Unrecognized login/API paths may contain account IDs or tokens.
    return site + (path if path in known else "/<redacted-path>")


def browser_logging(context, recorder):
    pending = {}
    provider = recorder.connection.provider

    def requested(request):
        endpoint = browser_endpoint(provider, request)
        if endpoint:
            pending[request] = RequestLog(request.method, endpoint, recorder)

    def responded(response):
        if span := pending.get(response.request):
            span.fields["status"] = response.status

    def finished(request):
        if span := pending.pop(request, None):
            span.finish(status=span.fields.pop("status", None))

    def failed(request):
        if span := pending.pop(request, None):
            span.finish(error_code="network")

    listeners = {
        "request": requested,
        "response": responded,
        "requestfinished": finished,
        "requestfailed": failed,
    }
    for name, callback in listeners.items():
        context.on(name, callback)

    def close():
        for name, callback in listeners.items():
            context.remove_listener(name, callback)
        for span in pending.values():
            span.finish(error_code="cancelled")
        pending.clear()

    return close


def format_log(row):
    stamp = datetime.fromtimestamp(row["created"], UTC).isoformat(timespec="milliseconds")
    fields = row["fields"]
    details = " ".join(f"{key}={value}" for key, value in fields.items())
    return f"{stamp} {row['connection']} job={row['job'] or '-'} {row['event']} {details}".rstrip()
