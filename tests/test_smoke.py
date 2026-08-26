from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from ibkr_tws_http.smoke import EXPECTED_API_OPERATIONS, SmokeFailure, _parser, run_smoke


class FakeClient:
    def __init__(self, *, connected: bool = True, chains: list[dict[str, Any]] | None = None):
        self.connected = connected
        self.chains = (
            chains
            if chains is not None
            else [
                {
                    "exchange": "SMART",
                    "underlying_con_id": 265598,
                    "trading_class": "AAPL",
                    "multiplier": "100",
                    "expirations": ["20260918", "20260925"],
                    "strikes": [195.0, 200.0, 205.0],
                },
                {
                    "exchange": "CBOE",
                    "underlying_con_id": 265598,
                    "trading_class": "AAPL",
                    "multiplier": "100",
                    "expirations": ["20260918"],
                    "strikes": [195.0, 200.0],
                },
            ]
        )
        self.requests: list[tuple[str, str, Mapping[str, Any] | None]] = []

    def get(self, path: str) -> Any:
        self.requests.append(("GET", path, None))
        if path == "/healthz":
            return {"service": "ok"}
        if path == "/v1/status":
            return {"tws_connected": self.connected, "server_version": 204}
        if path == "/openapi.json":
            return {
                "info": {"version": "0.9.0"},
                "paths": {
                    endpoint: {method: {}} for endpoint, method in EXPECTED_API_OPERATIONS.items()
                },
            }
        if path == "/v1/accounts":
            return [{"account": "account"}]
        if path == "/v1/account-summary":
            return [
                {
                    "account": "account",
                    "tag": "NetLiquidation",
                    "value": "10000",
                    "currency": "USD",
                }
            ]
        if path == "/v1/positions":
            return []
        if path == "/v1/contracts/search?pattern=AAPL":
            return [
                {
                    "contract": {
                        "con_id": 416904,
                        "symbol": "AAPL",
                        "sec_type": "FUT",
                        "exchange": "CME",
                        "currency": "USD",
                    },
                    "derivative_security_types": ["FOP"],
                },
                {
                    "contract": {
                        "con_id": 265598,
                        "symbol": "AAPL",
                        "sec_type": "STK",
                        "exchange": "SMART",
                        "primary_exchange": "NASDAQ",
                        "currency": "USD",
                    },
                    "derivative_security_types": ["OPT"],
                },
            ]
        if path == (
            "/v1/options/chains?underlying_symbol=AAPL&underlying_con_id=265598"
            "&underlying_security_type=STK&exchange="
        ):
            return self.chains
        if path == (
            "/v1/options/chain-snapshot?symbol=AAPL&expiration=20260918&exchange=SMART"
            "&market_data_type=delayed&strike_min=195.0&strike_max=205.0&max_contracts=6"
        ) or path == (
            "/v1/options/chain-snapshot?symbol=AAPL&expiration=20260918&exchange=SMART"
            "&market_data_type=auto&strike_min=195.0&strike_max=205.0&max_contracts=6"
        ):
            return {
                "complete": False,
                "market_quote_complete": False,
                "quote_complete": False,
                "model_complete": False,
                "enrichment_complete": False,
                "collection_timeout_seconds": 10,
                "max_concurrency": 10,
                "preflight_attempted": path.find("market_data_type=auto") >= 0,
                "preflight_market_data_type": "delayed",
                "preflight_seconds": 0.2,
                "underlying_snapshot": {
                    "complete": True,
                    "market_quote_complete": True,
                    "quote_complete": True,
                    "model_complete": None,
                    "enrichment_complete": True,
                    "market_data_type": "live",
                    "bid": 199.5,
                    "ask": 199.6,
                    "last": 199.55,
                },
                "underlying_error": None,
                "quotes": [
                    {
                        "contract": {"right": "C"},
                        "snapshot": {
                            "complete": True,
                            "market_quote_complete": True,
                            "quote_complete": True,
                            "model_complete": True,
                            "enrichment_complete": True,
                            "bid": 1.95,
                            "ask": 2.1,
                        },
                    },
                    {
                        "contract": {"right": "P"},
                        "snapshot": None,
                        "error": {"code": 354, "message": "not subscribed"},
                    },
                ],
            }
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path: str, payload: Mapping[str, Any]) -> Any:
        self.requests.append(("POST", path, payload))
        if path == "/v1/contracts/details":
            return [{"con_id": 265598, "symbol": "AAPL", "sec_type": "STK"}]
        if path == "/v1/market-data/snapshot":
            return {
                "requested_market_data_type": payload["market_data_type"],
                "market_data_type": (
                    "delayed" if payload["market_data_type"] == "delayed" else "live"
                ),
                "complete": True,
                "market_quote_complete": True,
                "quote_complete": True,
                "model_complete": None,
                "enrichment_complete": True,
                "collected_at": "2026-08-14T14:00:00Z",
                "bid": 199.5,
                "ask": 199.6,
                "last": 199.55,
            }
        raise AssertionError(f"unexpected POST {path}")


def test_smoke_exercises_live_read_only_flow_without_exposing_account_ids() -> None:
    client = FakeClient()
    messages: list[str] = []

    result = run_smoke(client, "aapl", messages.append)

    assert result.api_version == "0.9.0"
    assert result.server_version == 204
    assert result.account_count == 1
    assert result.account_summary_count == 1
    assert result.position_count == 0
    assert result.symbol == "AAPL"
    assert result.underlying_con_id == 265598
    assert result.chain_count == 2
    assert result.expiration_count == 2
    assert result.strike_count == 3
    assert result.quoted_contract_count == 1
    assert result.underlying_market_data_type == "live"
    assert messages[-1] == "Batch option snapshot succeeded (1/2 contracts priced, requested auto)"
    assert all("DU111" not in message for message in messages)
    assert (
        "POST",
        "/v1/contracts/details",
        {
            "con_id": 265598,
            "symbol": "AAPL",
            "sec_type": "STK",
            "exchange": "SMART",
            "currency": "USD",
            "primary_exchange": "NASDAQ",
        },
    ) in client.requests
    assert (
        "GET",
        "/v1/options/chains?underlying_symbol=AAPL&underlying_con_id=265598"
        "&underlying_security_type=STK&exchange=",
        None,
    ) in client.requests


def test_smoke_fails_before_tws_requests_when_disconnected() -> None:
    client = FakeClient(connected=False)

    with pytest.raises(SmokeFailure, match="not connected to TWS"):
        run_smoke(client, "AAPL")

    assert [request[:2] for request in client.requests] == [
        ("GET", "/healthz"),
        ("GET", "/v1/status"),
    ]


def test_smoke_requires_a_non_empty_option_chain() -> None:
    client = FakeClient(chains=[{"underlying_con_id": 265598, "expirations": [], "strikes": []}])

    with pytest.raises(SmokeFailure, match="no non-empty option chain"):
        run_smoke(client, "AAPL")


def test_smoke_can_require_a_real_batch_option_price() -> None:
    client = FakeClient()
    messages: list[str] = []

    result = run_smoke(
        client,
        "AAPL",
        messages.append,
        check_market_data=True,
        expiration="20260918",
        market_data_type="delayed",
    )

    assert result.quoted_contract_count == 1
    assert (
        messages[-1] == "Batch option snapshot succeeded (1/2 contracts priced, requested delayed)"
    )


def test_market_data_checks_are_enabled_without_cli_arguments() -> None:
    args = _parser().parse_args([])

    assert args.check_market_data is True
    assert args.timeout == 60.0
    assert _parser().parse_args(["--skip-market-data"]).check_market_data is False
