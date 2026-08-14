from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from decimal import Decimal

from fastapi.testclient import TestClient

from ibkr_tws_http.app import create_app
from ibkr_tws_http.config import ExposureConfig, ServerConfig, Settings, TwsConfig
from ibkr_tws_http.gateway import (
    AccountValue,
    ContractInfo,
    ContractSearchResult,
    GatewayRequestError,
    GatewayStatus,
    GatewayTimeout,
    HistoricalBar,
    MarketDataSnapshot,
    OptionChain,
    OptionComputation,
    Position,
)
from ibkr_tws_http.models import MarketDataType


def contract(symbol: str = "AAPL", sec_type: str = "STK") -> ContractInfo:
    return ContractInfo(
        con_id=265598,
        symbol=symbol,
        sec_type=sec_type,
        exchange="SMART",
        primary_exchange="NASDAQ",
        currency="USD",
        local_symbol=symbol,
        trading_class="NMS",
        long_name="Apple Inc.",
        min_tick=0.01,
    )


def option_contract(strike: float, right: str) -> ContractInfo:
    return ContractInfo(
        con_id=829_402_000 + int(strike * 10) + (1 if right == "C" else 2),
        symbol="AAPL",
        sec_type="OPT",
        exchange="SMART",
        primary_exchange=None,
        currency="USD",
        local_symbol=f"AAPL  260918{right}{int(strike * 1000):08d}",
        trading_class="AAPL",
        last_trade_date_or_contract_month="20260918",
        strike=strike,
        right=right,
        multiplier="100",
    )


class FakeGateway:
    def __init__(self) -> None:
        self.started = False
        self.summary_tags: frozenset[str] | None = None
        self.last_contract = None
        self.option_chain_requests = []

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    def status(self) -> GatewayStatus:
        return GatewayStatus(True, 204, "20260814 12:00:00 UTC")

    async def accounts(self) -> list[str]:
        return ["DU111", "DU222"]

    async def account_summary(self, tags: frozenset[str]) -> list[AccountValue]:
        self.summary_tags = tags
        return [
            AccountValue("DU111", "NetLiquidation", "10000", "USD"),
            AccountValue("DU111", "AccruedCash", "5", "USD"),
            AccountValue("DU222", "NetLiquidation", "90000", "USD"),
        ]

    async def positions(self) -> list[Position]:
        return [
            Position("DU111", contract(), Decimal("3.5"), 191.25),
            Position("DU111", contract("EUR", "CASH"), Decimal("100"), 1.1),
            Position("DU222", contract(), Decimal("80"), 150.0),
        ]

    async def contract_details(self, requested):
        self.last_contract = requested
        if requested.sec_type == "OPT":
            return [
                option_contract(strike, right)
                for strike in (195.0, 200.0, 205.0)
                for right in ("C", "P")
            ]
        return [contract(), contract("MSFT")]

    async def search_contracts(self, pattern: str) -> list[ContractSearchResult]:
        return [
            ContractSearchResult(contract(), ("OPT", "FUT")),
            ContractSearchResult(contract("MSFT"), ("OPT",)),
        ]

    async def option_chains(self, request) -> list[OptionChain]:
        self.option_chain_requests.append(request)
        if request.exchange:
            return []
        return [
            OptionChain(
                exchange="SMART",
                underlying_con_id=request.underlying_con_id,
                trading_class="AAPL",
                multiplier="100",
                expirations=("20260918", "20260925"),
                strikes=(195.0, 200.0, 205.0),
            )
        ]

    async def market_data_snapshot(self, request):
        self.last_contract = request.contract
        delayed = request.market_data_type is MarketDataType.DELAYED
        prefix = "delayed_" if delayed else ""
        computations = {}
        if request.contract.sec_type == "OPT":
            computations["model"] = OptionComputation(
                option_price=2.05,
                implied_volatility=0.75,
                delta=0.55 if request.contract.right == "C" else -0.45,
                gamma=0.08,
                theta=-0.01,
                vega=0.02,
                underlying_price=200.0,
                present_value_dividend=0.0,
            )
        enrichment_complete = request.contract.sec_type != "OPT" or not delayed
        return MarketDataSnapshot(
            market_data_type=(MarketDataType.DELAYED if delayed else MarketDataType.LIVE),
            as_of=datetime(2026, 8, 14, 14, 0, tzinfo=UTC),
            quote_complete=True,
            enrichment_complete=enrichment_complete,
            complete=enrichment_complete,
            fields={
                f"{prefix}bid": 199.5,
                f"{prefix}ask": 199.6,
                f"{prefix}last": 199.55,
                f"{prefix}bid_size": 100,
                f"{prefix}ask_size": 120,
                f"{prefix}last_size": 5,
                f"{prefix}volume": 1000,
                f"{prefix}last_timestamp": "1786714800",
                "option_call_open_interest": 2500,
                "option_put_open_interest": 1250,
            },
            option_computations=computations,
        )

    async def historical_bars(self, request):
        self.last_contract = request.contract
        return [
            HistoricalBar(
                "20260814 12:00:00",
                198.0,
                200.0,
                197.5,
                199.5,
                Decimal("1000"),
                Decimal("199.2"),
                75,
            )
        ]


def settings(**exposure_overrides) -> Settings:
    exposure_values = {
        "allowed_accounts": frozenset({"DU111"}),
        "account_aliases": {"DU111": "paper"},
        "account_summary_tags": frozenset({"NetLiquidation"}),
        "allowed_security_types": frozenset({"STK", "OPT"}),
        "allowed_symbols": frozenset({"AAPL"}),
    }
    exposure_values.update(exposure_overrides)
    exposure = ExposureConfig(**exposure_values)
    return Settings(server=ServerConfig(), exposure=exposure)


def test_read_only_routes_apply_response_and_contract_policy() -> None:
    gateway = FakeGateway()
    with TestClient(create_app(settings(), gateway)) as client:
        assert gateway.started
        assert client.get("/healthz").json() == {"service": "ok"}
        assert client.get("/v1/status").json() == {
            "service": "ok",
            "tws_connected": True,
            "server_version": 204,
            "connection_time": "20260814 12:00:00 UTC",
            "read_only": True,
        }
        assert client.get("/v1/accounts").json() == [{"account": "paper"}]
        assert client.get("/v1/account-summary").json() == [
            {
                "account": "paper",
                "tag": "NetLiquidation",
                "value": "10000",
                "currency": "USD",
            }
        ]
        positions = client.get("/v1/positions").json()
        assert len(positions) == 1
        assert positions[0]["account"] == "paper"
        assert positions[0]["contract"]["symbol"] == "AAPL"

        response = client.post(
            "/v1/contracts/details",
            json={"symbol": "aapl", "sec_type": "stk", "exchange": "smart"},
        )
        assert response.status_code == 200
        assert [item["symbol"] for item in response.json()] == ["AAPL"]
        assert gateway.last_contract.symbol == "AAPL"

        response = client.get("/v1/contracts/search", params={"pattern": "apple"})
        assert response.status_code == 200
        assert response.json() == [
            {
                "contract": {
                    "con_id": 265598,
                    "symbol": "AAPL",
                    "sec_type": "STK",
                    "exchange": "SMART",
                    "primary_exchange": "NASDAQ",
                    "currency": "USD",
                    "local_symbol": "AAPL",
                    "trading_class": "NMS",
                    "long_name": "Apple Inc.",
                    "min_tick": 0.01,
                    "last_trade_date_or_contract_month": None,
                    "strike": None,
                    "right": None,
                    "multiplier": None,
                },
                "derivative_security_types": ["OPT"],
            }
        ]

        response = client.get(
            "/v1/options/chains",
            params={"underlying_symbol": "aapl", "underlying_con_id": 265598},
        )
        assert response.status_code == 200
        assert response.json() == [
            {
                "exchange": "SMART",
                "underlying_con_id": 265598,
                "trading_class": "AAPL",
                "multiplier": "100",
                "expirations": ["20260918", "20260925"],
                "strikes": [195.0, 200.0, 205.0],
            }
        ]

        response = client.post(
            "/v1/market-data/snapshot",
            json={"contract": {"symbol": "aapl"}, "market_data_type": "delayed"},
        )
        assert response.status_code == 200
        snapshot = response.json()
        assert snapshot["requested_market_data_type"] == "delayed"
        assert snapshot["market_data_type"] == "delayed"
        assert snapshot["quote_complete"] is True
        assert snapshot["enrichment_complete"] is True
        assert snapshot["complete"] is True
        assert snapshot["collected_at"] == "2026-08-14T14:00:00Z"
        assert "as_of" not in snapshot
        assert snapshot["bid"] == 199.5
        assert snapshot["ask_size"] == 120
        assert snapshot["volume"] == 1000
        assert snapshot["last_trade_time"] == "2026-08-14T13:40:00Z"

        response = client.get(
            "/v1/options/chain-snapshot",
            params={
                "symbol": "aapl",
                "expiration": "20260918",
                "market_data_type": "delayed",
            },
        )
        assert response.status_code == 200
        chain_snapshot = response.json()
        assert chain_snapshot["underlying"]["con_id"] == 265598
        assert chain_snapshot["underlying_snapshot"]["last"] == 199.55
        assert chain_snapshot["underlying_error"] is None
        assert chain_snapshot["requested_market_data_type"] == "delayed"
        assert chain_snapshot["quote_complete"] is True
        assert chain_snapshot["enrichment_complete"] is False
        assert chain_snapshot["complete"] is False
        assert chain_snapshot["collection_started_at"] <= chain_snapshot["collected_at"]
        assert len(chain_snapshot["quotes"]) == 6
        assert chain_snapshot["quotes"][0]["contract"]["strike"] == 195.0
        assert (
            chain_snapshot["quotes"][0]["snapshot"]["option_computations"]["model"][
                "implied_volatility"
            ]
            == 0.75
        )
        assert chain_snapshot["quotes"][0]["snapshot"]["open_interest"] == 2500
        assert chain_snapshot["quotes"][1]["snapshot"]["open_interest"] == 1250
        assert gateway.option_chain_requests[-1].exchange == ""

        response = client.post(
            "/v1/historical-bars",
            json={"contract": {"symbol": "aapl"}},
        )
        assert response.status_code == 200
        assert response.json()[0]["volume"] == "1000"

        openapi = client.get("/openapi.json").json()
        assert openapi["info"]["version"] == "0.5.0"
        assert set(openapi["paths"]) == {
            "/healthz",
            "/v1/status",
            "/v1/accounts",
            "/v1/account-summary",
            "/v1/positions",
            "/v1/contracts/details",
            "/v1/contracts/search",
            "/v1/options/chains",
            "/v1/options/chain-snapshot",
            "/v1/market-data/snapshot",
            "/v1/historical-bars",
        }
        assert set(openapi["paths"]["/v1/options/chains"]) == {"get"}
        market_data_schema = openapi["components"]["schemas"]["MarketDataSnapshotResponse"]
        assert {
            "quote_complete",
            "enrichment_complete",
            "complete",
            "collected_at",
        } <= set(market_data_schema["properties"])
        assert "as_of" not in market_data_schema["properties"]
        assert (
            openapi["paths"]["/v1/market-data/snapshot"]["post"]["responses"]["504"]["content"][
                "application/json"
            ]["schema"]["$ref"]
            == "#/components/schemas/GatewayTimeoutResponse"
        )
        assert "securitySchemes" not in openapi["components"]
    assert not gateway.started


def test_rejects_out_of_policy_and_naive_historical_time() -> None:
    with TestClient(create_app(settings(), FakeGateway())) as client:
        denied = client.post(
            "/v1/market-data/snapshot",
            json={"contract": {"symbol": "MSFT"}},
        )
        assert denied.status_code == 403
        assert denied.json() == {"detail": "symbol is not allowed"}

        denied_chain = client.get(
            "/v1/options/chains",
            params={"underlying_symbol": "MSFT", "underlying_con_id": 272093},
        )
        assert denied_chain.status_code == 403
        assert denied_chain.json() == {"detail": "symbol is not allowed"}

        invalid = client.post(
            "/v1/historical-bars",
            json={"contract": {"symbol": "AAPL"}, "end": "2026-08-14T12:00:00"},
        )
        assert invalid.status_code == 422


def test_disabled_feature_is_not_exposed() -> None:
    with TestClient(create_app(settings(features=frozenset({"status"})), FakeGateway())) as client:
        response = client.get("/v1/positions")
    assert response.status_code == 403
    assert response.json() == {"detail": "feature 'positions' is disabled"}


def test_option_chains_require_option_security_type() -> None:
    restricted = settings(allowed_security_types=frozenset({"STK"}))
    with TestClient(create_app(restricted, FakeGateway())) as client:
        response = client.get(
            "/v1/options/chains",
            params={"underlying_symbol": "AAPL", "underlying_con_id": 265598},
        )
    assert response.status_code == 403
    assert response.json() == {"detail": "security type 'OPT' is not allowed"}


class ErrorGateway(FakeGateway):
    async def positions(self) -> list[Position]:
        raise GatewayRequestError("account DU111 is invalid", code=354)

    async def market_data_snapshot(self, request):
        raise GatewayRequestError("No market data for DU111", code=354)


def test_upstream_error_details_are_not_returned() -> None:
    with TestClient(create_app(settings(), ErrorGateway())) as client:
        response = client.get("/v1/positions")
    assert response.status_code == 502
    assert response.json() == {
        "detail": "TWS rejected the request",
        "tws_error": {"code": 354, "message": "account [redacted account] is invalid"},
    }
    assert "DU111" not in response.text

    with TestClient(create_app(settings(), ErrorGateway())) as client:
        response = client.post(
            "/v1/market-data/snapshot",
            json={"contract": {"symbol": "AAPL"}},
        )
        openapi = client.get("/openapi.json").json()
    assert response.status_code == 502
    assert response.json()["tws_error"] == {
        "code": 354,
        "message": "No market data for [redacted account]",
    }
    assert (
        openapi["paths"]["/v1/market-data/snapshot"]["post"]["responses"]["502"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        == "#/components/schemas/TwsErrorResponse"
    )


class PartialQuoteErrorGateway(FakeGateway):
    async def market_data_snapshot(self, request):
        if request.contract.right == "P":
            raise GatewayRequestError("No market data for DU111", code=354)
        return await super().market_data_snapshot(request)


class PartialQuoteTimeoutGateway(FakeGateway):
    async def market_data_snapshot(self, request):
        if request.contract.right == "P":
            raise GatewayTimeout("request 42 timed out")
        return await super().market_data_snapshot(request)


class HangingQuoteGateway(FakeGateway):
    def __init__(self) -> None:
        super().__init__()
        self.active_quotes = 0
        self.max_active_quotes = 0

    async def market_data_snapshot(self, request):
        self.active_quotes += 1
        self.max_active_quotes = max(self.max_active_quotes, self.active_quotes)
        try:
            await asyncio.sleep(10)
        finally:
            self.active_quotes -= 1


def test_batch_snapshot_keeps_per_contract_errors_and_redacts_accounts() -> None:
    with TestClient(create_app(settings(), PartialQuoteErrorGateway())) as client:
        response = client.get(
            "/v1/options/chain-snapshot",
            params={"symbol": "AAPL", "expiration": "20260918"},
        )
    assert response.status_code == 200
    payload = response.json()
    quotes = payload["quotes"]
    assert payload["complete"] is False
    assert payload["underlying_snapshot"] is not None
    assert len([quote for quote in quotes if quote["snapshot"] is not None]) == 3
    errors = [quote["error"] for quote in quotes if quote["error"] is not None]
    assert errors == [{"code": 354, "message": "No market data for [redacted account]"}] * 3
    assert "DU111" not in response.text


def test_batch_snapshot_keeps_per_contract_timeouts() -> None:
    with TestClient(create_app(settings(), PartialQuoteTimeoutGateway())) as client:
        response = client.get(
            "/v1/options/chain-snapshot",
            params={"symbol": "AAPL", "expiration": "20260918"},
        )

    assert response.status_code == 200
    assert response.json()["complete"] is False
    errors = [quote["error"] for quote in response.json()["quotes"] if quote["error"]]
    assert errors == [{"code": None, "message": "TWS market data request timed out"}] * 3


def test_batch_snapshot_uses_one_concurrent_collection_deadline() -> None:
    gateway = HangingQuoteGateway()
    base_settings = settings()
    deadline_settings = Settings(
        server=base_settings.server,
        tws=TwsConfig(request_timeout_seconds=0.05),
        exposure=base_settings.exposure,
    )

    started_at = time.monotonic()
    with TestClient(create_app(deadline_settings, gateway)) as client:
        response = client.get(
            "/v1/options/chain-snapshot",
            params={"symbol": "AAPL", "expiration": "20260918"},
        )
    elapsed = time.monotonic() - started_at

    assert response.status_code == 200
    payload = response.json()
    assert payload["quote_complete"] is False
    assert payload["enrichment_complete"] is False
    assert payload["complete"] is False
    assert payload["underlying_error"] == {
        "code": None,
        "message": "batch market data collection deadline reached",
    }
    assert {(quote["error"]["code"], quote["error"]["message"]) for quote in payload["quotes"]} == {
        (None, "batch market data collection deadline reached")
    }
    assert gateway.max_active_quotes == 7
    assert gateway.active_quotes == 0
    assert elapsed < 1
