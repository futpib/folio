from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .binance import BinanceConnector
from .broker import ServiceBroker
from .browser import BrowserConnector
from .calls import CallRequest
from .config import Connection, Settings
from .ibkr import IbkrConnector
from .models import ConnectorError, SyncRequest
from .observability import Recorder, RequestLog, current, recording
from .store import Store, exclusive


def utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


async def worker(store: Store, connection: Connection, connector, broker=None, *, lock=None):
    lock = lock or asyncio.Lock()
    with exclusive(store.directory / f"{connection.id}.worker.lock"):
        store.recover(connection.id)
        while True:
            job = store.claim(connection.id)
            if job is None:
                await asyncio.sleep(0.25)
                continue
            async with lock:
                with recording(store, connection, job["id"]) as log:
                    log.emit("job_started")
                    await run_job(store, connection, connector, broker, job, log)


async def run_job(store, connection, connector, broker, job, log):
    try:
        result = await connector.sync(job["since"])
        store.finish(job, result, connection.min_sync_seconds)
        log.emit(
            "job_completed",
            records=len(result.records),
            complete=result.complete,
            warnings=len(result.warnings),
        )
    except ConnectorError as error:
        cooldown = (
            0
            if error.code == "login_required"
            else max(connection.min_sync_seconds, error.retry_after)
        )
        store.fail(job, error.code, str(error), cooldown)
        log.emit("job_failed", error_code=error.code)
    except asyncio.CancelledError:
        store.fail(
            job,
            "interrupted",
            "Worker stopped during sync; stored data is unchanged.",
            connection.min_sync_seconds,
        )
        log.emit("job_failed", error_code="interrupted")
        raise
    except Exception:
        # Browser errors can contain URLs, headers or page content. Never echo them.
        store.fail(
            job,
            "connector_failed",
            "Connector failed; check installation or sign in again. Stored data is unchanged.",
            max(300, connection.min_sync_seconds),
        )
        log.emit("job_failed", error_code="connector_failed")
    finally:
        if isinstance(broker, ServiceBroker):
            broker.close()


def create_app(settings: Settings, *, connectors=None) -> FastAPI:
    store = Store(settings.state_dir)
    configured = {c.id: c for c in settings.connections}
    locks = {name: asyncio.Lock() for name in configured}
    brokers = {
        c.id: ServiceBroker(settings, c)
        for c in settings.connections
        if c.credential_adapter and c.provider != "ibkr"
    }
    for connection in configured.values():
        store.register(connection)
    connectors = (
        connectors
        if connectors is not None
        else {
            c.id: (
                IbkrConnector(c)
                if c.provider == "ibkr"
                else BinanceConnector(c, brokers.get(c.id))
                if c.provider == "binance"
                else BrowserConnector(settings, c, brokers.get(c.id))
            )
            for c in settings.connections
        }
    )

    @asynccontextmanager
    async def lifespan(app):
        with exclusive(settings.state_dir / "daemon.lock"):
            async with AsyncExitStack() as stack:
                for connector in connectors.values():
                    if isinstance(connector, IbkrConnector):
                        await stack.enter_async_context(
                            connector.app.router.lifespan_context(connector.app)
                        )
                async with asyncio.TaskGroup() as tasks:
                    running = [
                        tasks.create_task(
                            worker(store, c, connectors[c.id], brokers.get(c.id), lock=locks[c.id])
                        )
                        for c in settings.connections
                    ]
                    try:
                        yield
                    finally:
                        for task in running:
                            task.cancel()

    app = FastAPI(
        title="Folio",
        version=__version__,
        lifespan=lifespan,
        description="Local read access to personal accounts and purchases.",
    )
    app.state.store = store
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"]
    )

    @app.middleware("http")
    async def local_origin(request: Request, call_next):
        source = request.headers.get("origin")
        if source is not None and source != f"http://{request.headers.get('host')}":
            return JSONResponse(status_code=403, content={"detail": "cross-origin access refused"})
        return await call_next(request)

    def connection(name):
        if name not in configured:
            raise HTTPException(404, "unknown connection")
        return configured[name]

    @app.get("/healthz")
    async def health():
        return {"service": "ok", "version": __version__}

    @app.get("/v1/connections")
    async def connections():
        return [
            {
                **store.status(c.id),
                "resources": c.resources,
                "worker": "foliod",
                "authentication": "gateway"
                if c.provider == "ibkr"
                else "env-first, zond-if-missing"
                if c.credential_adapter
                else "env/browser",
                "browser_support": "experimental" if c.provider in {"bog", "wolt"} else None,
                **(
                    {
                        "api_support": "binance-rest",
                        "markets": c.binance.markets,
                        "account_mode": c.binance.account_mode,
                        "history_days": c.binance.history_days,
                        "trade_symbols": {
                            m: getattr(c.binance, f"{m}_symbols") for m in c.binance.markets
                        },
                    }
                    if c.binance
                    else {}
                ),
                "live_api": f"/v1/connections/{c.id}/ibkr" if c.provider == "ibkr" else None,
                "call_api": f"/v1/connections/{c.id}/call",
            }
            for c in settings.connections
        ]

    @app.post("/v1/connections/{name}/sync", status_code=202)
    async def sync(name: str, request: SyncRequest):
        connection(name)
        return store.queue(name, None)

    @app.post("/v1/connections/{name}/call")
    async def call(name: str, spec: CallRequest, request: Request):
        selected = connection(name)
        ident = str(uuid4())

        async def run():
            async with locks[name]:
                with recording(store, selected, call_id=ident) as log:
                    log.emit("call_started")
                    try:
                        result = await connectors[name].call(spec)
                        log.emit("call_completed", status=result.status)
                        return {
                            "id": ident,
                            "status": result.status,
                            "content_type": result.content_type,
                            "body": result.body,
                            "logs": log.items,
                        }
                    except ValueError as error:
                        log.emit("call_failed", error_code="invalid_call")
                        raise HTTPException(400, str(error)) from None
                    except ConnectorError as error:
                        log.emit("call_failed", error_code=error.code)
                        return JSONResponse(
                            status_code=502,
                            content={
                                "id": ident,
                                "error": str(error),
                                "error_code": error.code,
                                "logs": log.items,
                            },
                        )
                    except asyncio.CancelledError:
                        log.emit("call_failed", error_code="cancelled")
                        raise
                    except Exception:
                        log.emit("call_failed", error_code="connector_failed")
                        return JSONResponse(
                            status_code=502,
                            content={
                                "id": ident,
                                "error": "Provider call failed; check connectivity "
                                "or sign in again.",
                                "error_code": "connector_failed",
                                "logs": log.items,
                            },
                        )
                    finally:
                        if isinstance(brokers.get(name), ServiceBroker):
                            brokers[name].close()

        task = asyncio.create_task(run())

        async def disconnected():
            while (await request.receive())["type"] != "http.disconnect":
                pass

        watcher = asyncio.create_task(disconnected())
        try:
            done, _ = await asyncio.wait({task, watcher}, return_when=asyncio.FIRST_COMPLETED)
            if task not in done:
                raise HTTPException(499, "caller disconnected")
            return await task
        finally:
            for pending in (task, watcher):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(task, watcher, return_exceptions=True)

    @app.get("/v1/jobs/{ident}")
    async def job(ident: str):
        result = store.job(ident)
        if result is None or result["connection"] not in configured:
            raise HTTPException(404, "unknown job")
        return result

    @app.get("/v1/connections/{name}/logs")
    async def logs(
        name: str,
        after: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=1000)] = 1000,
        job: str | None = None,
    ):
        connection(name)
        if job is not None:
            result = store.job(job)
            if result is None or result["connection"] != name:
                raise HTTPException(404, "unknown job")
        return store.logs(name, after=after, limit=limit, job=job)

    @app.get("/v1/connections/{name}/records/{resource}")
    async def read(
        name: str,
        resource: str,
        since: datetime | None = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ):
        if resource not in connection(name).resources:
            raise HTTPException(404, "resource is not supported by this connection")
        return store.read(name, resource, utc(since), limit, offset)

    first_ibkr = None
    for name, connector in connectors.items():
        if isinstance(connector, IbkrConnector):
            ibkr_logging(connector.app, store, configured[name])
            app.mount(f"/v1/connections/{name}/ibkr", connector.app)
            if first_ibkr is None:
                first_ibkr = connector.app
    # Preserve existing /v1/status, quotes, options and account routes on the same port.
    if first_ibkr is not None:
        app.mount("/", first_ibkr)
    return app


def ibkr_logging(app, store, connection):
    from starlette.routing import Match

    @app.middleware("http")
    async def request_log(request, call_next):
        route = next(
            (route for route in app.routes if route.matches(request.scope)[0] == Match.FULL), None
        )
        endpoint = "ibkr://gateway" + (route.path if route else "/<unknown-route>")
        recorder = current.get() or Recorder(store, connection)
        with RequestLog(request.method, endpoint, recorder) as log:
            response = await call_next(request)
            log.finish(status=response.status_code)
            return response
