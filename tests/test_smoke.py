from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from ibkr_tws_http.smoke import SmokeFailure, run_smoke


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
        if path == "/v1/accounts":
            return [{"account": "account"}]
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
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path: str, payload: Mapping[str, Any]) -> Any:
        self.requests.append(("POST", path, payload))
        if path == "/v1/contracts/details":
            return [{"con_id": 265598, "symbol": "AAPL", "sec_type": "STK"}]
        raise AssertionError(f"unexpected POST {path}")


def test_smoke_exercises_live_read_only_flow_without_exposing_account_ids() -> None:
    client = FakeClient()
    messages: list[str] = []

    result = run_smoke(client, "aapl", messages.append)

    assert result.server_version == 204
    assert result.account_count == 1
    assert result.position_count == 0
    assert result.symbol == "AAPL"
    assert result.underlying_con_id == 265598
    assert result.chain_count == 2
    assert result.expiration_count == 2
    assert result.strike_count == 3
    assert messages[-1] == "Option chains request succeeded (2 chains, 2 expirations, 3 strikes)"
    assert all("account" not in message.lower() or "1 exposed" in message for message in messages)
    assert client.requests[-2] == (
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
    )
    assert client.requests[-1] == (
        "GET",
        "/v1/options/chains?underlying_symbol=AAPL&underlying_con_id=265598"
        "&underlying_security_type=STK&exchange=",
        None,
    )


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
