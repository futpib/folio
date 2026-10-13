from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from binance_fixture import KEY, NOW, SECRET, Api, trade
from fastapi.testclient import TestClient

from folio.binance import BinanceConnector, Credentials
from folio.config import BinanceSettings, Connection, Settings
from folio.service import create_app


def service(tmp_path, api, *, mode="auto"):
    connection = Connection("binance", "binance", binance=BinanceSettings(account_mode=mode))
    connector = BinanceConnector(connection, transport=httpx.MockTransport(api.handle), pace=0)
    connector.credentials = Credentials(KEY, SECRET)
    connector.clocks = {market: (NOW, time.monotonic()) for market in ("spot", "usdm", "coinm")}
    app = create_app(Settings(tmp_path, (connection,)), connectors={"binance": connector})
    return app, connector


def test_live_trade_pages_preserve_native_fields_and_leave_collection_to_caller(tmp_path):
    api = Api()
    api.account_type = "PM_2"
    api.history = [trade("BTCUSDT", i) for i in (1, 2, 3)]
    app, _ = service(tmp_path, api)
    with TestClient(app) as client:
        params = {"market": "usdm", "account_mode": "portfolio", "symbol": "BTCUSDT", "limit": 2}

        def read(**filters):
            response = client.get(
                "/v1/connections/binance/live/trades", params={**params, **filters}
            )
            assert response.status_code == 200, response.text
            return response.json()

        first = read()
        assert [x["id"] for x in first["items"]] == [1, 2]
        assert first["items"][0]["qty"] == "0.001"
        assert first["items"][0]["price"] == "123.4567890123456789"
        assert first["items"][0]["positionSide"] == "LONG"
        assert first["resource"] == "trades" and first["source"] == "live"
        assert first["next_cursor"] == {"from_id": 3} and first["page_full"]
        assert len(api.requests) == 1
        second = read(**first["next_cursor"])
        assert [x["id"] for x in second["items"]] == [3] and not second["page_full"]
        assert second["next_cursor"] == {"from_id": 4}
        final = read(**second["next_cursor"])
        assert final["items"] == [] and final["next_cursor"] is None
        assert len(api.requests) == 3
        assert all(path == "/papi/v1/um/userTrades" for path, _ in api.requests)
        assert [p["fromId"] for _, p in api.requests] == ["0", "3", "4"]
        assert client.get("/v1/connections/binance/records/trades").json()["snapshot"] is None
        with app.state.store.db() as db:
            assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0


@pytest.mark.parametrize(
    "kind,mode,prefix",
    [
        (None, "classic", "/fapi/v1"),
        ("PM_2", "portfolio", "/papi/v1/um"),
        ("PM_1", "portfolio_pro", "/fapi/v1"),
        ("PM_3", "portfolio_pro_span", "/fapi/v1"),
    ],
)
def test_auto_mode_uses_one_metadata_lookup_then_one_page_and_bound_hint_skips_lookup(
    tmp_path, kind, mode, prefix
):
    api = Api()
    api.account_type = kind
    app, connector = service(tmp_path, api)
    with TestClient(app) as client:
        params = {"market": "usdm", "symbol": "BTCUSDT"}
        first = client.get("/v1/connections/binance/live/trades", params=params).json()
        assert first["ok"] and first["scope"]["account_mode"] == mode
        assert [p for p, _ in api.requests] == [
            "/sapi/v1/portfolio/account",
            prefix + "/userTrades",
        ]
        binding = hashlib.sha256(KEY.encode()).hexdigest()
        with app.state.store.db() as db:
            db.execute("INSERT INTO connection_credentials VALUES (?,?)", ("binance", binding))
            db.execute(
                "INSERT INTO snapshots VALUES (?,?,?,?,?)",
                ("binance", "accounts", time.time(), 1, f"account mode={mode} | range finished"),
            )
        api.requests.clear()
        next_page = client.get("/v1/connections/binance/live/trades", params=params).json()
        assert next_page["ok"] and len(api.requests) == 1
        assert api.requests[0][0] == prefix + "/userTrades"
        # A changed key must not inherit another key's remembered mode.
        with app.state.store.db() as db:
            db.execute("UPDATE connection_credentials SET credential_identity='other-key'")
        api.requests.clear()
        assert client.get("/v1/connections/binance/live/trades", params=params).json()["ok"]
        assert len(api.requests) == 2
        assert connector.credentials is not None


def test_time_window_is_explicit_single_page_with_exclusive_upper_bound(tmp_path):
    api = Api()
    app, _ = service(tmp_path, api)
    end = datetime.fromtimestamp(NOW / 1000, UTC) + timedelta(milliseconds=1)
    start = end - timedelta(days=7)
    with TestClient(app) as client:
        result = client.get(
            "/v1/connections/binance/live/trades",
            params={
                "market": "usdm",
                "symbol": "BTCUSDT",
                "account_mode": "classic",
                "since": start.isoformat(),
                "until": end.isoformat(),
            },
        ).json()
        assert result["ok"] and len(api.requests) == 1
        assert api.requests[0][1]["endTime"] == str(NOW)
        assert (
            int(api.requests[0][1]["endTime"]) - int(api.requests[0][1]["startTime"])
            == 7 * 86400000 - 1
        )
        assert "fromId" not in api.requests[0][1]
        assert result["next_cursor"] is None and result["scope"]["pagination"] == "time_window"


@pytest.mark.parametrize(
    "extra",
    [
        {"url": "https://evil.test"},
        {"endpoint": "/api/v3/account"},
        {"headers": "secret"},
        {"from_id": -1},
        {"symbol": "../escape"},
        {"limit": 1001},
        {"since": "2026-09-01", "until": "2026-09-09"},
        {"since": "2026-09-01", "until": "2026-09-02", "from_id": 0},
        {"since": "2026-09-01"},
        {"page": 2},
        {"cursor": "anything"},
    ],
)
def test_named_read_filters_reject_raw_paths_and_invalid_history_requests_before_auth(
    tmp_path, extra
):
    api = Api()
    app, connector = service(tmp_path, api)
    connector.credentials = None
    with TestClient(app) as client:
        result = client.get(
            "/v1/connections/binance/live/trades",
            params={"market": "usdm", "symbol": "BTCUSDT", "account_mode": "portfolio", **extra},
        )
        assert result.status_code in {400, 422}, result.text
        assert not api.requests and connector.credentials is None


def test_permission_error_is_a_provider_result_and_does_not_fall_back_or_sync(tmp_path):
    api = Api()
    api.account_type = "PM_2"
    api.failure = lambda req: httpx.Response(
        401, json={"code": -2015, "msg": "Permission denied " + KEY + SECRET}
    )
    app, _ = service(tmp_path, api)
    with TestClient(app) as client:
        result = client.get(
            "/v1/connections/binance/live/trades",
            params={"market": "usdm", "account_mode": "portfolio", "symbol": "BTCUSDT"},
        ).json()
        assert not result["ok"] and result["status"] == 401 and result["error"]["code"] == -2015
        assert "Permission denied" in result["error"]["msg"]
        assert KEY not in json.dumps(result) and SECRET not in json.dumps(result)
        assert len(api.requests) == 1


def test_native_balance_objects_are_extracted_without_combining_markets(tmp_path):
    api = Api()
    api.account_type = "PM_2"
    app, _ = service(tmp_path, api)
    with TestClient(app) as client:
        result = client.get(
            "/v1/connections/binance/live/balances",
            params={"market": "usdm", "account_mode": "portfolio"},
        ).json()
        assert result["ok"] and len(api.requests) == 1
        assert result["items"][0]["crossWalletBalance"] == "23.724692060001"
        assert "totalWalletBalance" not in result["items"][0]
        assert "availableBalance" not in result["items"][0]
