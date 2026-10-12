from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from dataclasses import replace
from urllib.parse import parse_qs

import httpx
import pytest
from binance_fixture import KEY, SECRET
from fastapi.testclient import TestClient

from folio.binance import BinanceConnector, Credentials
from folio.calls import CallRequest, CallResult
from folio.config import Connection, Settings
from folio.ibkr import IbkrConnector
from folio.models import Snapshot
from folio.service import create_app
from ibkr_tws_http.app import create_app as ibkr_app
from ibkr_tws_http.config import ServerConfig
from ibkr_tws_http.config import Settings as IbkrSettings
from ibkr_tws_http.mock_tws import create_mock_gateway


@pytest.mark.parametrize(
    "path,host",
    [
        ("/api/v3/newRead", "api.binance.com"),
        ("/sapi/v1/newRead", "api.binance.com"),
        ("/fapi/v1/newRead", "fapi.binance.com"),
        ("/dapi/v1/newRead", "dapi.binance.com"),
        ("/papi/v1/um/newRead", "papi.binance.com"),
    ],
)
def test_single_signed_arbitrary_endpoint_has_no_sync_or_clock_probe(tmp_path, path, host):
    sent = []

    def handle(request):
        sent.append(request)
        assert request.url.host == host
        encoded, signature = request.url.query.decode().rsplit("&signature=", 1)
        expected = hmac.new(SECRET.encode(), encoded.encode(), hashlib.sha256).hexdigest()
        assert signature == expected
        assert request.headers["X-MBX-APIKEY"] == KEY
        params = parse_qs(encoded)
        assert params["symbol"] == ["ETHUSDT"]
        assert params["tag"] == ["one", "two"]
        assert abs(int(params["timestamp"][0]) - time.time() * 1000) < 1000
        assert params["recvWindow"] == ["5000"]
        return httpx.Response(200, content='{"price":123.4567890123456789012345,"next":"more"}')

    selected = Connection("binance", "binance")
    connector = BinanceConnector(selected, transport=httpx.MockTransport(handle), pace=0)
    connector.credentials = Credentials(KEY, SECRET)
    app = create_app(Settings(tmp_path, (selected,)), connectors={"binance": connector})
    with TestClient(app) as client:
        response = client.post(
            "/v1/connections/binance/call",
            json={
                "endpoint": path + "?tag=one",
                "params": [["symbol", "ETHUSDT"], ["tag", "two"]],
            },
        )
        assert response.status_code == 200
        result = response.json()
        assert result["body"] == '{"price":123.4567890123456789012345,"next":"more"}'
        assert result["status"] == 200
        assert len(sent) == 1
        assert client.get("/v1/connections/binance/records/trades").json()["snapshot"] is None
        with app.state.store.db() as db:
            assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
        logs = client.get("/v1/connections/binance/logs").json()["items"]
        assert logs == result["logs"]
        assert all(row["fields"]["call_id"] == result["id"] for row in logs)
        assert KEY not in json.dumps(logs) and SECRET not in json.dumps(logs)
        assert "ETHUSDT" not in json.dumps(logs) and "123.456789" not in json.dumps(logs)


@pytest.mark.parametrize("status", [200, 401, 429, 302])
def test_raw_error_status_and_body_are_returned_without_following_or_retrying(status):
    sent = []

    def handle(request):
        sent.append(request)
        signature = request.url.params["signature"]
        return httpx.Response(
            status,
            json={
                "code": -2015,
                "msg": f"Permission denied, key={KEY}, secret={SECRET}, signature={signature}",
            },
            headers={"Location": "https://elsewhere.test", "Retry-After": "60"},
        )

    connector = BinanceConnector(
        Connection("binance", "binance"), transport=httpx.MockTransport(handle), pace=0
    )
    connector.credentials = Credentials(KEY, SECRET)
    result = asyncio.run(connector.call(CallRequest(endpoint="/papi/v1/um/userTrades")))
    assert result.status == status and len(sent) == 1
    assert json.loads(result.body)["code"] == -2015
    assert "Permission denied" in result.body
    assert KEY not in result.body and SECRET not in result.body
    assert "<redacted>" in result.body
    assert connector.credentials is not None


def test_public_call_needs_no_credentials_and_updates_clock_for_next_signed_call(tmp_path):
    seen = []

    def handle(request):
        seen.append(request)
        if request.url.path.endswith("/time"):
            assert "X-MBX-APIKEY" not in request.headers
            assert "signature" not in request.url.params
            return httpx.Response(200, json={"serverTime": 1792000000000})
        assert 1792000000000 <= int(request.url.params["timestamp"]) < 1792000010000
        return httpx.Response(200, json=[])

    env = tmp_path / ".env.local"
    connector = BinanceConnector(
        Connection("binance", "binance"),
        env_file=env,
        transport=httpx.MockTransport(handle),
        pace=0,
    )
    assert asyncio.run(connector.call(CallRequest(endpoint="/api/v3/time"))).status == 200
    env.write_text(f"BINANCE_API_KEY={KEY}\nBINANCE_API_SECRET={SECRET}\n")
    assert asyncio.run(connector.call(CallRequest(endpoint="/papi/v1/um/userTrades"))).status == 200
    assert len(seen) == 2


@pytest.mark.parametrize(
    "spec",
    [
        {"endpoint": "https://attacker.test/api/v3/account"},
        {"endpoint": "http://api.binance.com/api/v3/account"},
        {"endpoint": "https://api.binance.com:444/api/v3/account"},
        {"endpoint": "https://key@api.binance.com/api/v3/account"},
        {"endpoint": "/api/v3/account#fragment"},
        {"endpoint": "/api/v3/account?signature=secret"},
        {"endpoint": "/api/v3/account", "params": [["timestamp", "1"]]},
        {"endpoint": "/api/v3/account", "params": [["api_key", "secret"]]},
        {"endpoint": "/api/v3/../account"},
        {"endpoint": "/api/v3/%2e%2e/account"},
        {"endpoint": "/api/v3/account", "method": "POST"},
    ],
)
def test_invalid_calls_fail_before_loading_credentials_or_contacting_provider(tmp_path, spec):
    def unexpected(_):
        raise AssertionError("invalid call must not reach the network")

    connector = BinanceConnector(
        Connection("binance", "binance"),
        env_file=tmp_path / ".env.local",
        transport=httpx.MockTransport(unexpected),
    )
    with pytest.raises(ValueError):
        asyncio.run(connector.call(CallRequest(**spec)))
    assert connector.credentials is None


def test_ibkr_calls_keep_exposure_policy_and_accept_post_read_queries(tmp_path):
    old = IbkrSettings(server=ServerConfig())
    old = replace(old, exposure=replace(old.exposure, allowed_accounts=frozenset({"DU424242"})))
    connection = Connection("ibkr", "ibkr")
    connector = IbkrConnector(connection, app=ibkr_app(old, create_mock_gateway(old.tws)))
    with TestClient(
        create_app(Settings(tmp_path, (connection,)), connectors={"ibkr": connector})
    ) as client:

        def call(endpoint, **kwargs):
            return client.post(
                "/v1/connections/ibkr/call", json={"endpoint": endpoint, **kwargs}
            ).json()

        result = call("/v1/positions")
        assert result["status"] == 200 and "DU424242" not in result["body"]
        assert len(json.loads(result["body"])) == 2
        result = call(
            "/v1/contracts/details",
            method="POST",
            body='{"symbol":"AAPL","sec_type":"STK","exchange":"SMART","currency":"USD"}',
        )
        assert result["status"] == 200
        assert call("/v1/orders", method="POST", body="{}")["status"] == 404
        assert (
            client.post(
                "/v1/connections/ibkr/call", json={"endpoint": "http://outside/v1/positions"}
            ).status_code
            == 400
        )


def test_calls_and_syncs_share_connection_lock(tmp_path):
    class Connector:
        active = 0
        highest = 0

        async def execute(self):
            self.active += 1
            self.highest = max(self.highest, self.active)
            await asyncio.sleep(0.05)
            self.active -= 1

        async def call(self, spec):
            await self.execute()
            return CallResult(200, "{}")

        async def sync(self, since):
            await self.execute()
            return Snapshot(records=[], resources=["orders"], complete=True, coverage="test")

    connector = Connector()
    app = create_app(
        Settings(tmp_path, (Connection("wolt", "wolt", min_sync_seconds=1),)),
        connectors={"wolt": connector},
    )

    async def run():
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client,
        ):
            await client.post("/v1/connections/wolt/sync", json={})
            await asyncio.gather(
                *[
                    client.post("/v1/connections/wolt/call", json={"endpoint": "/test"})
                    for _ in range(3)
                ]
            )
            await asyncio.sleep(0.3)
        assert connector.highest == 1

    asyncio.run(run())
