from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from binance_fixture import KEY, NOW, SECRET, Api, income, trade
from fastapi.testclient import TestClient

from folio.binance import BinanceConnector, Credentials
from folio.config import BinanceSettings, Connection, Settings
from folio.models import ConnectorError, LoginRequired, RateLimited, SchemaChanged
from folio.service import create_app
from folio.store import Store


def connection(**overrides):
    settings = BinanceSettings(
        markets=("spot", "usdm", "coinm"),
        spot_symbols=("BTCUSDT",),
        usdm_symbols=("BTCUSDT",),
        coinm_symbols=("BTCUSD_PERP",),
        history_days=1,
    )
    return Connection(
        "binance", "binance", credential_adapter=True, binance=replace(settings, **overrides)
    )


def connector(api, configured=None, broker=None):
    result = BinanceConnector(
        configured or connection(), broker, transport=httpx.MockTransport(api.handle), pace=0
    )
    if broker is None:
        result.credentials = Credentials(KEY, SECRET)
    return result


def test_signed_spot_and_both_futures_markets_preserve_values_and_identity():
    api = Api()
    result = asyncio.run(connector(api).sync(None))
    assert result.complete and result.account_identity == "binance:42"
    assert len([r for r in result.records if r.resource == "positions"]) == 4
    spot = next(r for r in result.records if r.id == "spot:USDT")
    assert spot.data["free"] == "123.0000000000001"
    trades = [r for r in result.records if r.resource == "trades"]
    assert len(trades) == 3
    assert trades[0].data["price"] == "123.4567890123456789"
    assert {r.data["quantity_unit"] for r in trades} == {"base_asset", "contracts"}
    assert len([r for r in result.records if r.resource == "income"]) == 4
    assert len({r.id for r in result.records if r.resource == "income"}) == 4
    payload = result.model_dump_json()
    assert KEY not in payload and SECRET not in payload and "never persist" not in payload
    assert "BTCUSD_PERP" in result.coverage and "configured markets only" in result.coverage


def test_trade_windows_split_full_pages_without_losing_same_timestamp_rows(monkeypatch):
    monkeypatch.setattr("folio.binance.PAGE_SIZE", 3)
    api = Api()
    api.history = [trade("BTCUSDT", i, NOW - 1000 * i) for i in range(1, 8)]
    api.history += [trade("BTCUSDT", 8, NOW - 1000)]
    result = asyncio.run(
        connector(api, connection(markets=("spot",), usdm_symbols=(), coinm_symbols=())).sync(None)
    )
    assert result.complete
    trades = [r for r in result.records if r.resource == "trades"]
    assert {r.data["trade_id"] for r in trades} == {str(i) for i in range(1, 9)}
    assert len(trades) == 8


def test_history_bounds_are_honest_and_balances_survive_page_limit(monkeypatch):
    monkeypatch.setattr("folio.binance.PAGE_SIZE", 2)
    api = Api()
    api.history = [trade("BTCUSDT", i) for i in range(4)]
    configured = replace(
        connection(markets=("spot",), usdm_symbols=(), coinm_symbols=()), max_pages=1
    )
    result = asyncio.run(connector(api, configured).sync(None))
    assert not result.complete and "incomplete" in result.coverage
    assert len([r for r in result.records if r.resource == "balances"]) == 1


def test_income_pages_keep_one_range_and_detect_a_repeated_page(monkeypatch):
    monkeypatch.setattr("folio.binance.PAGE_SIZE", 2)
    api = Api()
    configured = connection(markets=("usdm",), spot_symbols=(), usdm_symbols=(), coinm_symbols=())

    def paginated(request):
        if request.url.path.endswith("income"):
            page = int(request.url.params["page"])
            return httpx.Response(200, json=[income(1), income(2)] if page == 1 else [income(3)])
        return None

    api.failure = paginated
    result = asyncio.run(connector(api, configured).sync(None))
    assert result.complete
    assert len([r for r in result.records if r.resource == "income"]) == 3
    pages = [params for path, params in api.requests if path.endswith("income")]
    assert [p["page"] for p in pages] == ["1", "2"]
    assert len({(p["startTime"], p["endTime"]) for p in pages}) == 1
    api.failure = None
    with pytest.raises(SchemaChanged, match="did not advance"):
        asyncio.run(connector(api, configured).sync(None))


@pytest.mark.parametrize(
    "status,code,expected",
    [
        (401, -2015, LoginRequired),
        (401, -2014, LoginRequired),
        (400, -1022, LoginRequired),
        (400, -1021, ConnectorError),
        (403, -1000, ConnectorError),
        (500, -1000, ConnectorError),
        (451, -1000, ConnectorError),
    ],
)
def test_errors_are_safe_and_auth_rejection_discards_credentials(status, code, expected):
    api = Api()
    api.failure = lambda _: httpx.Response(status, json={"code": code, "msg": KEY + SECRET})
    worker = connector(api)
    with pytest.raises(expected) as error:
        asyncio.run(worker.sync(None))
    assert KEY not in str(error.value) and SECRET not in str(error.value)
    assert f"HTTP {status}, code {code}" in str(error.value)
    assert "spot account (/api/v3/account" in str(error.value)
    if expected is LoginRequired:
        assert worker.credentials is None


@pytest.mark.parametrize("code", [-2014, -2015, -1022])
def test_futures_auth_error_identifies_failing_market_and_remedy(code):
    api = Api()
    api.failure = lambda request: (
        httpx.Response(401, json={"code": code, "msg": KEY + SECRET})
        if request.url.path.startswith("/fapi/")
        else None
    )
    with pytest.raises(LoginRequired) as error:
        asyncio.run(connector(api).sync(None))
    message = str(error.value)
    assert f"usdm account (/fapi/v3/account, HTTP 401, code {code})" in message
    assert {
        -2014: "BAD_API_KEY_FMT",
        -2015: "IP allowlist",
        -1022: "matches the public key",
    }[code] in message
    assert KEY not in message and SECRET not in message


@pytest.mark.parametrize("code", [[-2015], {"secret": KEY}, KEY, True, 10**30, None])
def test_untrusted_error_code_is_not_echoed_or_used_as_a_mapping_key(code):
    api = Api()
    api.failure = lambda _: httpx.Response(401, json={"code": code, "msg": KEY + SECRET})
    with pytest.raises(LoginRequired) as error:
        asyncio.run(connector(api).sync(None))
    assert "HTTP 401): Unauthorized request" in str(error.value)
    assert KEY not in str(error.value) and SECRET not in str(error.value)


def test_rate_limit_honors_long_bans_and_redirects_never_forward_keys():
    api = Api()
    api.failure = lambda _: httpx.Response(418, headers={"Retry-After": "172800"})
    with pytest.raises(RateLimited) as error:
        asyncio.run(connector(api).sync(None))
    assert error.value.retry_after == 172800
    api.failure = lambda _: httpx.Response(302, headers={"Location": "https://attacker.test"})
    with pytest.raises(ConnectorError, match="redirect"):
        asyncio.run(connector(api).sync(None))
    assert len(api.requests) == 2


def test_failed_page_keeps_previous_snapshot_and_changed_account_clears_old_history(tmp_path):
    api = Api()
    configured = connection()
    store = Store(tmp_path)
    store.register(configured)
    job = store.queue(configured.id, None)
    store.finish(job, asyncio.run(connector(api).sync(None)), 0)
    before = store.read(configured.id, "trades", None, 100, 0)
    api.failure = lambda r: (
        httpx.Response(200, json=[{"broken": KEY + SECRET}])
        if r.url.path.endswith("income")
        else None
    )
    with pytest.raises(SchemaChanged):
        asyncio.run(connector(api).sync(None))
    assert store.read(configured.id, "trades", None, 100, 0) == before
    api.failure = None
    api.uid = 43
    configured = replace(configured, binance=BinanceSettings())
    result = asyncio.run(connector(api, configured).sync(None))
    job = store.queue(configured.id, None)
    store.finish(job, result, 0)
    assert store.read(configured.id, "trades", None, 100, 0)["items"] == []
    assert store.read(configured.id, "balances", None, 100, 0)["items"]


class Broker:
    def __init__(self, values):
        self.values, self.results, self.challenges = values, [], 0

    async def challenge(self, provider, fields, origin):
        self.challenges += 1
        self.fields = fields
        assert all(f["kind"] == "secret" for f in fields)
        assert "https://api.binance.com" in origin
        return "challenge"

    async def receive(self):
        return {"type": "answered", "challenge_id": "challenge", "values": self.values}

    async def result(self, ident, outcome):
        self.results.append(outcome)


def test_credentials_use_broker_once_and_are_reused_only_in_memory():
    broker = Broker({"api_key": KEY, "api_secret": SECRET})
    worker = connector(Api(), broker=broker)
    asyncio.run(worker.sync(None))
    asyncio.run(worker.sync(None))
    assert broker.values == {} and broker.results == ["succeeded"] and broker.challenges == 1
    assert KEY not in repr(worker.credentials)
    declined = connector(Api(), broker=Broker(None))
    with pytest.raises(LoginRequired, match="declined"):
        asyncio.run(declined.sync(None))
    assert declined.credentials is None and declined.broker.results == []


@pytest.mark.parametrize("present", [(), ("api_key",), ("api_secret",), ("api_key", "api_secret")])
def test_env_credentials_prompt_only_for_missing_fields(tmp_path, present):
    expected = {"api_key": KEY, "api_secret": SECRET}
    path = tmp_path / ".env.local"
    if present:
        path.write_text("".join(f"BINANCE_{name.upper()}={expected[name]}\n" for name in present))
    broker = Broker({name: value for name, value in expected.items() if name not in present})
    worker = connector(Api(), broker=broker)
    assert asyncio.run(worker.sync(None)).account_identity == "binance:42"
    assert broker.challenges == (len(present) < 2)
    if broker.challenges:
        assert [f["id"] for f in broker.fields] == [
            name for name in expected if name not in present
        ]
        assert KEY not in json.dumps(broker.fields) and SECRET not in json.dumps(broker.fields)
    assert (
        not path.exists()
        if not present
        else all(
            f"BINANCE_{name.upper()}" not in path.read_text()
            for name in expected
            if name not in present
        )
    )


def test_rejected_file_credentials_are_reread_without_prompting(tmp_path):
    path = tmp_path / ".env.local"
    path.write_text(f"BINANCE_API_KEY=WrongFixtureApiKey123\nBINANCE_API_SECRET={SECRET}\n")
    broker = Broker(None)
    worker = connector(Api(), broker=broker)
    with pytest.raises(LoginRequired):
        asyncio.run(worker.sync(None))
    assert worker.credentials is None and broker.challenges == 0
    path.write_text(f"BINANCE_API_KEY={KEY}\nBINANCE_API_SECRET={SECRET}\n")
    assert asyncio.run(worker.sync(None)).account_identity == "binance:42"
    assert broker.challenges == 0


def test_config_and_api_expose_only_fixed_reads(tmp_path):
    path = tmp_path / "config.toml"
    valid = '[connections.binance]\nprovider="binance"\ncredential_adapter=true\n'
    path.write_text(valid)
    settings = replace(Settings.load(path), state_dir=tmp_path / "state")
    with TestClient(create_app(settings)) as client:
        info = client.get("/v1/connections").json()[0]
        assert info["worker"] == "foliod" and info["browser_support"] is None
        assert info["markets"] == ["spot", "usdm"]
        assert info["resources"] == ["accounts", "balances", "positions", "trades", "income"]
        assert (
            client.post("/v1/connections/binance/sync", json={"api_secret": SECRET}).status_code
            == 422
        )
        assert client.post("/v1/connections/binance/orders", json={}).status_code == 404
    for invalid in (
        valid.replace("true", "42"),
        valid + 'api_key="no"',
        valid + '[connections.binance.binance]\nurl="https://attacker.test"',
        valid + '[connections.binance.binance]\nmarkets=["wrong"]',
        valid + '[connections.binance.binance]\nkey_type="ecdsa"',
        valid + "[connections.binance.binance]\nkey_type={}",
        valid + "[connections.binance.binance]\nmarkets=[{}]",
        valid + "[connections.binance.binance]\nhistory_days=91",
        valid + '[connections.binance.binance]\nspot_symbols=["BTCUSDT", "BTCUSDT"]',
    ):
        path.write_text(invalid)
        with pytest.raises(ValueError):
            Settings.load(path)
    assert SECRET not in json.dumps(info)
