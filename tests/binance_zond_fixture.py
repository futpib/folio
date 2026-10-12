"""Run the actual API adapter CLI against an isolated loopback Binance fixture."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
from binance_fixture import Api
from cryptography.hazmat.primitives.serialization import load_pem_public_key

import folio.binance as binance
from folio.cli import main

api = Api()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        response = api.handle(
            httpx.Request("GET", "http://localhost" + self.path, headers=dict(self.headers))
        )
        body = response.content
        self.send_response(response.status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    public = Path.cwd() / "fixture-public.pem"
    if public.exists():
        api.public_key = load_pem_public_key(public.read_bytes())
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    for market in binance.ORIGINS:
        binance.ORIGINS[market] = f"http://127.0.0.1:{server.server_port}"
    # Preserve the production constructor and CLI; only eliminate fixture pacing.
    original = binance.BinanceConnector.__init__

    def initialize(self, *args, **kwargs):
        original(self, *args, **kwargs, pace=0, env_file=Path.cwd() / ".env.local")

    binance.BinanceConnector.__init__ = initialize
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        main()
    finally:
        server.shutdown()
        server.server_close()
