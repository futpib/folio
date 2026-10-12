from __future__ import annotations

import asyncio

import httpx
import pytest
from binance_fixture import KEY, SECRET, Api
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from test_binance import connection, connector

from folio.binance import Credentials, credential_origins
from folio.config import BinanceSettings
from folio.models import ConnectorError, LoginRequired, RateLimited, SchemaChanged
from folio.store import Store


@pytest.mark.parametrize("kind", ["hmac", "ed25519", "rsa"])
@pytest.mark.parametrize(
    "account_type,mode",
    [
        (None, "classic"),
        ("PM_2", "portfolio"),
        ("PM_1", "portfolio_pro"),
        ("PM_3", "portfolio_pro_span"),
    ],
)
def test_auto_mode_routes_all_reads_and_verifies_signatures(kind, account_type, mode):
    private = None
    if kind != "hmac":
        private = (
            ed25519.Ed25519PrivateKey.generate()
            if kind == "ed25519"
            else rsa.generate_private_key(public_exponent=65537, key_size=2048)
        )
    api = Api(private.public_key() if private else None)
    api.account_type = account_type
    worker = connector(api)
    worker.credentials = (
        Credentials.from_values(
            {
                "api_key": KEY,
                "private_key": private.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                ).decode(),
            },
            kind,
        )
        if private
        else Credentials(KEY, SECRET)
    )
    result = asyncio.run(worker.sync(None))
    assert result.complete and not result.warnings
    assert result.account_identity == "binance:42"
    assert f"account mode={mode}" in result.coverage
    positions = [r for r in result.records if r.resource == "positions"]
    assert len(positions) == 4 and {r.data["account_mode"] for r in positions} == {mode}
    assert {r.data["quantity_unit"] for r in positions} == {"base_asset", "contracts"}
    assert len([r for r in result.records if r.resource == "trades"]) == 3
    assert len([r for r in result.records if r.resource == "income"]) == 4
    paths = {p for p, _ in api.requests}
    if mode == "portfolio":
        assert {
            f"/papi/v1/{m}/{op}"
            for m in ("um", "cm")
            for op in ("account", "positionRisk", "userTrades", "income")
        } <= paths
        assert not any(p.startswith(("/fapi", "/dapi")) for p in paths)
        balance = next(r for r in result.records if r.id == "usdm:USDT")
        assert balance.data["crossWalletBalance"] == "23.724692060001"
        assert "availableBalance" not in balance.data
    else:
        assert "/fapi/v3/positionRisk" in paths and "/dapi/v1/positionRisk" in paths
        assert not any(p.startswith("/papi") for p in paths)
    if account_type:
        balance = next(r for r in result.records if r.id == "portfolio:USDT")
        assert balance.data["totalWalletBalance"] == "126.724692060001"
        assert "cmUnrealizedPNL" not in balance.data
        assert balance.data["balance_scope"] == "unified"
        account = next(r for r in result.records if r.id == "portfolio:portfolio")
        assert account.data["accountEquity"] == "73.474280580001"
        assert "totalAvailableBalance" not in account.data
    if mode == "portfolio_pro_span":
        assert "/sapi/v2/portfolio/account" in paths
        assert account.data["risk_units"] == [
            {"asset": "BTC", "maintenance_margin_usd": "23.72469206"}
        ]
    assert "never persist" not in result.model_dump_json()
    assert KEY not in result.model_dump_json() and SECRET not in result.model_dump_json()


def test_papi_metadata_is_used_when_sapi_is_denied():
    api = Api()
    api.account_type = "PM_2"
    api.failure = lambda r: (
        httpx.Response(401, json={"code": -2015})
        if r.url.path == "/sapi/v1/portfolio/account"
        else None
    )
    result = asyncio.run(connector(api).sync(None))
    assert result.complete and "account mode=portfolio" in result.coverage
    assert len([r for r in result.records if r.resource == "positions"]) == 4


def test_denied_metadata_does_not_hide_readable_positions_or_delete_unified_cache(tmp_path):
    api = Api()
    api.account_type = "PM_2"
    configured = connection()
    worker = connector(api, configured)
    store = Store(tmp_path)
    store.register(configured)

    def save():
        job = store.queue("binance", None)
        store.finish(job, asyncio.run(worker.sync(None)), 0)
        return store.job(job["id"])

    assert save()["complete"]
    before = store.read("binance", "balances", None, 100, 0)
    denied = {
        "/sapi/v1/portfolio/account",
        "/papi/v1/account",
        "/papi/v1/balance",
        "/sapi/v1/portfolio/balance",
    }
    api.failure = lambda r: (
        httpx.Response(401, json={"code": -2015}) if r.url.path in denied else None
    )
    partial = save()
    assert partial["state"] == "completed" and not partial["complete"]
    assert worker.credentials is not None
    after = store.read("binance", "balances", None, 100, 0)
    assert [r for r in before["items"] if r["market"] == "portfolio"] == [
        r for r in after["items"] if r["market"] == "portfolio"
    ]
    assert next(s for s in after["scopes"] if s["market"] == "portfolio")["status"] == "denied"
    positions = store.read("binance", "positions", None, 100, 0)
    assert len(positions["items"]) == 4
    assert {s["status"] for s in positions["scopes"]} == {"complete"}
    assert {r["account_mode"] for r in positions["items"]} == {"portfolio"}
    api.failure = None
    assert save()["complete"]


@pytest.mark.parametrize(
    "mode,account_type",
    [
        ("classic", None),
        ("portfolio", "PM_2"),
        ("portfolio_pro", "PM_1"),
        ("portfolio_pro_span", "PM_3"),
    ],
)
def test_explicit_modes_skip_discovery(mode, account_type):
    api = Api()
    api.account_type = account_type
    result = asyncio.run(connector(api, connection(account_mode=mode)).sync(None))
    assert result.complete
    paths = [p for p, _ in api.requests]
    assert ("/sapi/v1/portfolio/account" in paths) == (mode == "portfolio_pro")
    assert ("/sapi/v2/portfolio/account" in paths) == (mode == "portfolio_pro_span")


@pytest.mark.parametrize(
    "status,code,expected",
    [(429, None, RateLimited), (400, -1022, LoginRequired), (500, -1000, ConnectorError)],
)
def test_discovery_failures_do_not_silently_select_another_mode(status, code, expected):
    api = Api()
    api.failure = lambda r: (
        httpx.Response(status, json={"code": code})
        if r.url.path == "/sapi/v1/portfolio/account"
        else None
    )
    with pytest.raises(expected):
        asyncio.run(connector(api).sync(None))
    assert not any(p.startswith(("/papi", "/fapi", "/dapi")) for p, _ in api.requests)


def test_unknown_account_types_fail_without_assuming_classic():
    api = Api()
    api.account_type = "PM_FUTURE_TYPE"
    with pytest.raises(SchemaChanged, match="unknown Portfolio Margin account type"):
        asyncio.run(connector(api).sync(None))


@pytest.mark.parametrize(
    "mode", ["auto", "classic", "portfolio", "portfolio_pro", "portfolio_pro_span"]
)
def test_configuration_and_recipient_origins_cover_selected_mode(mode):
    options = BinanceSettings.load({"account_mode": mode})
    assert options.account_mode == mode
    assert ("https://papi.binance.com" in credential_origins(options)) == (
        mode in {"auto", "portfolio"}
    )
    assert "https://api.binance.com" in credential_origins(options)
    for invalid in ("new_mode", [], {}, None):
        with pytest.raises(ValueError, match="account_mode"):
            BinanceSettings.load({"account_mode": invalid})
