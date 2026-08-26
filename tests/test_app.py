from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
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
    TwsNotice,
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
        self.market_data_requests = []
        self.historical_requests = []

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
        self.market_data_requests.append(request)
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
            market_quote_complete=True,
            model_complete=True,
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
        self.historical_requests.append(request)
        if request.data_type == "OPTION_IMPLIED_VOLATILITY":
            prices = (0.78, 0.82, 0.77, 0.8)
        elif request.data_type == "HISTORICAL_VOLATILITY":
            prices = (0.68, 0.72, 0.67, 0.7)
        else:
            prices = (198.0, 200.0, 197.5, 199.5)
        return [
            HistoricalBar(
                "20260814 12:00:00",
                *prices,
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
        assert client.get("/v1/capabilities").json() == {
            "tws_connected": True,
            "live_market_quotes": {"status": "unknown", "evidence_codes": []},
            "delayed_option_model": {"status": "unknown", "evidence_codes": []},
            "exact_option_historical_data": {"status": "unknown", "evidence_codes": []},
            "transient_option_history_enabled": True,
            "transient_option_history_samples": 0,
            "adaptive_batch_scheduler": True,
            "continuous_uptime_required": False,
            "additional_market_data_subscriptions_required": False,
            "persistent_storage": False,
            "all_state_transient": True,
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
        assert snapshot["market_quote_complete"] is True
        assert snapshot["quote_complete"] is True
        assert snapshot["model_complete"] is None
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
                "timeout_seconds": 5,
            },
        )
        assert response.status_code == 200
        chain_snapshot = response.json()
        assert chain_snapshot["underlying"]["con_id"] == 265598
        assert chain_snapshot["underlying_snapshot"]["last"] == 199.55
        assert chain_snapshot["underlying_error"] is None
        assert chain_snapshot["requested_market_data_type"] == "delayed"
        assert chain_snapshot["market_quote_complete"] is True
        assert chain_snapshot["quote_complete"] is True
        assert chain_snapshot["model_complete"] is True
        assert chain_snapshot["enrichment_complete"] is False
        assert chain_snapshot["complete"] is False
        assert chain_snapshot["collection_timeout_seconds"] == 5
        assert chain_snapshot["retried_contracts"] == 0
        assert chain_snapshot["underlying_attempts"] == 1
        assert chain_snapshot["reference_underlying_price"] is None
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

        response = client.post(
            "/v1/volatility/history",
            json={"contract": {"symbol": "aapl"}, "duration": "2 W"},
        )
        assert response.status_code == 200
        volatility = response.json()
        assert volatility["contract"]["symbol"] == "AAPL"
        assert volatility["duration"] == "2 W"
        assert volatility["bar_size"] == "1 day"
        assert volatility["implied_volatility"] == {
            "data_type": "OPTION_IMPLIED_VOLATILITY",
            "name": "ib_30_day_at_market_implied_volatility",
            "window_calendar_days": 30,
            "units": "decimal",
            "bars": [
                {
                    "time": "20260814 12:00:00",
                    "open": 0.78,
                    "high": 0.82,
                    "low": 0.77,
                    "close": 0.8,
                    "volume": "1000",
                    "weighted_average_price": "199.2",
                    "trade_count": 75,
                }
            ],
        }
        assert volatility["historical_volatility"]["bars"][0]["close"] == 0.7
        assert {request.data_type for request in gateway.historical_requests[-2:]} == {
            "OPTION_IMPLIED_VOLATILITY",
            "HISTORICAL_VOLATILITY",
        }

        transient_history = client.post(
            "/v1/options/observed-history",
            json={"contract": {"con_id": 123, "symbol": "AAPL", "sec_type": "OPT"}},
        )
        assert transient_history.status_code == 200
        assert transient_history.json()["samples"] == []

        openapi = client.get("/openapi.json").json()
        assert openapi["info"]["version"] == "0.10.1"
        assert set(openapi["paths"]) == {
            "/healthz",
            "/v1/status",
            "/v1/capabilities",
            "/v1/accounts",
            "/v1/account-summary",
            "/v1/positions",
            "/v1/contracts/details",
            "/v1/contracts/search",
            "/v1/options/chains",
            "/v1/options/chain-snapshot",
            "/v1/options/portfolio-snapshot",
            "/v1/options/volatility-comparison",
            "/v1/options/observed-history",
            "/v1/market-data/snapshot",
            "/v1/historical-bars",
            "/v1/volatility/history",
        }
        assert set(openapi["paths"]["/v1/options/chains"]) == {"get"}
        market_data_schema = openapi["components"]["schemas"]["MarketDataSnapshotResponse"]
        assert {
            "market_quote_complete",
            "quote_complete",
            "model_complete",
            "enrichment_complete",
            "complete",
            "collected_at",
            "implied_volatility",
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

        invalid_volatility = client.post(
            "/v1/volatility/history",
            json={"contract": {"symbol": "AAPL", "sec_type": "OPT"}},
        )
        assert invalid_volatility.status_code == 422


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


class SideComputationGateway(FakeGateway):
    async def market_data_snapshot(self, request):
        if request.contract.sec_type != "OPT":
            return await super().market_data_snapshot(request)
        computation_values = {
            name: OptionComputation(
                option_price=price,
                implied_volatility=implied_volatility,
                delta=0.55,
                gamma=0.08,
                theta=-0.01,
                vega=0.02,
                underlying_price=200.0,
                present_value_dividend=0.0,
            )
            for name, price, implied_volatility in (
                ("bid", 1.7, 0.7),
                ("ask", 1.9, 0.8),
                ("last", 1.85, 0.76),
                ("model", 1.827, 0.75),
            )
        }
        collected_at = datetime(2026, 8, 14, 14, 0, tzinfo=UTC)
        return MarketDataSnapshot(
            market_data_type=MarketDataType.DELAYED,
            as_of=collected_at,
            market_quote_complete=True,
            model_complete=True,
            enrichment_complete=False,
            complete=False,
            fields={},
            option_computations=computation_values,
            option_computation_observed_at={name: collected_at for name in computation_values},
        )


class MissingUnderlyingGateway(FakeGateway):
    async def market_data_snapshot(self, request):
        if request.contract.sec_type != "OPT":
            raise GatewayTimeout("underlying timed out")
        return await super().market_data_snapshot(request)


class ModelOnlyOptionGateway(FakeGateway):
    async def market_data_snapshot(self, request):
        if request.contract.sec_type != "OPT":
            return await super().market_data_snapshot(request)
        return MarketDataSnapshot(
            market_data_type=MarketDataType.DELAYED,
            as_of=datetime(2026, 8, 14, 14, 0, tzinfo=UTC),
            market_quote_complete=False,
            model_complete=True,
            enrichment_complete=False,
            complete=False,
            fields={},
            option_computations={
                "model": OptionComputation(
                    option_price=1.827,
                    implied_volatility=0.75,
                    delta=0.55,
                    gamma=0.08,
                    theta=-0.01,
                    vega=0.02,
                    underlying_price=200.0,
                    present_value_dividend=0.0,
                )
            },
        )


class EntitlementPreflightGateway(FakeGateway):
    async def market_data_snapshot(self, request):
        if request.contract.sec_type == "OPT" and request.market_data_type is MarketDataType.AUTO:
            snapshot = await super().market_data_snapshot(
                request.model_copy(update={"market_data_type": MarketDataType.DELAYED})
            )
            return replace(snapshot, notices=(TwsNotice(2186, "using delayed data"),))
        return await super().market_data_snapshot(request)


class CrossedOptionGateway(SideComputationGateway):
    async def market_data_snapshot(self, request):
        snapshot = await super().market_data_snapshot(request)
        if request.contract.sec_type != "OPT":
            return snapshot
        computations = dict(snapshot.option_computations)
        computations["bid"] = replace(computations["bid"], option_price=1.9)
        computations["ask"] = replace(computations["ask"], option_price=1.7)
        return replace(snapshot, option_computations=computations)


class StaleOptionGateway(SideComputationGateway):
    async def market_data_snapshot(self, request):
        snapshot = await super().market_data_snapshot(request)
        if request.contract.sec_type != "OPT":
            return snapshot
        observed_at = snapshot.as_of - timedelta(seconds=6)
        return replace(
            snapshot,
            option_computation_observed_at={
                name: observed_at for name in snapshot.option_computations
            },
        )


class IvBoundsWithoutQuotesGateway(SideComputationGateway):
    async def market_data_snapshot(self, request):
        snapshot = await super().market_data_snapshot(request)
        if request.contract.sec_type != "OPT":
            return snapshot
        computations = dict(snapshot.option_computations)
        computations["bid"] = replace(computations["bid"], option_price=None)
        computations["ask"] = replace(computations["ask"], option_price=None)
        return replace(
            snapshot,
            market_quote_complete=False,
            option_computations=computations,
        )


def _option_snapshot(gateway: FakeGateway) -> dict:
    with TestClient(create_app(settings(), gateway)) as client:
        response = client.post(
            "/v1/market-data/snapshot",
            json={
                "contract": {
                    "symbol": "AAPL",
                    "sec_type": "OPT",
                    "last_trade_date_or_contract_month": "20260918",
                    "strike": 200,
                    "right": "C",
                },
                "market_data_type": "delayed",
            },
        )
    assert response.status_code == 200
    return response.json()


def test_option_iv_uses_only_a_fresh_non_crossed_market_mid() -> None:
    payload = _option_snapshot(SideComputationGateway())

    assert payload["implied_volatility"] == {
        "value": 0.75,
        "source": "market_mid",
        "bid_implied_volatility": 0.7,
        "ask_implied_volatility": 0.8,
        "quote_observed_at": "2026-08-14T14:00:00Z",
        "quote_age_seconds": 0.0,
        "spread_percentage": 11.111111111111109,
        "market_mid_eligible": True,
        "market_mid_rejection_reason": None,
        "max_quote_age_seconds": 5.0,
    }


def test_option_iv_falls_back_to_explicitly_labelled_model_value() -> None:
    model_only = _option_snapshot(ModelOnlyOptionGateway())["implied_volatility"]
    crossed = _option_snapshot(CrossedOptionGateway())["implied_volatility"]
    stale = _option_snapshot(StaleOptionGateway())["implied_volatility"]
    iv_without_quotes = _option_snapshot(IvBoundsWithoutQuotesGateway())["implied_volatility"]

    assert model_only["source"] == "model"
    assert model_only["value"] == 0.75
    assert model_only["market_mid_rejection_reason"] == "missing_bid_or_ask"
    assert crossed["source"] == "model"
    assert crossed["market_mid_rejection_reason"] == "crossed_quote"
    assert crossed["spread_percentage"] < 0
    assert stale["source"] == "model"
    assert stale["quote_age_seconds"] == 6.0
    assert stale["market_mid_rejection_reason"] == "stale_quote"
    assert iv_without_quotes["source"] == "model"
    assert iv_without_quotes["bid_implied_volatility"] == 0.7
    assert iv_without_quotes["ask_implied_volatility"] == 0.8
    assert iv_without_quotes["spread_percentage"] is None
    assert iv_without_quotes["market_mid_rejection_reason"] == "missing_bid_or_ask"


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
    assert payload["collection_timeout_seconds"] == 10
    assert payload["preflight_attempted"] is True
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
    payload = response.json()
    assert payload["complete"] is False
    assert payload["retried_contracts"] == 3
    errors = [quote["error"] for quote in payload["quotes"] if quote["error"]]
    assert errors == [{"code": None, "message": "TWS market data request timed out"}] * 3
    assert [quote["attempts"] for quote in payload["quotes"] if quote["error"]] == [2, 2, 2]


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
            params={
                "symbol": "AAPL",
                "expiration": "20260918",
                "market_data_type": "delayed",
                "max_concurrency": 3,
            },
        )
    elapsed = time.monotonic() - started_at

    assert response.status_code == 200
    payload = response.json()
    assert payload["market_quote_complete"] is False
    assert payload["quote_complete"] is False
    assert payload["model_complete"] is False
    assert payload["enrichment_complete"] is False
    assert payload["complete"] is False
    assert payload["retried_contracts"] == 7
    assert payload["max_concurrency"] == 3
    assert payload["preflight_attempted"] is False
    assert payload["underlying_attempts"] == 2
    assert payload["underlying_error"] == {
        "code": None,
        "message": "TWS market data request timed out",
    }
    assert {(quote["error"]["code"], quote["error"]["message"]) for quote in payload["quotes"]} == {
        (None, "TWS market data request timed out")
    }
    assert {quote["attempts"] for quote in payload["quotes"]} == {2}
    assert gateway.max_active_quotes == 3
    assert gateway.active_quotes == 0
    assert elapsed < 1


def test_option_side_computations_populate_top_level_prices() -> None:
    with TestClient(create_app(settings(), SideComputationGateway())) as client:
        response = client.post(
            "/v1/market-data/snapshot",
            json={
                "contract": {
                    "symbol": "AAPL",
                    "sec_type": "OPT",
                    "last_trade_date_or_contract_month": "20260918",
                    "strike": 200,
                    "right": "C",
                },
                "market_data_type": "delayed",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert (payload["bid"], payload["ask"], payload["last"]) == (1.7, 1.9, 1.85)
    assert payload["market_quote_complete"] is True
    assert payload["quote_complete"] is True
    assert payload["model_complete"] is True
    assert payload["enrichment_complete"] is False


def test_batch_retries_underlying_and_exposes_option_model_reference_price() -> None:
    with TestClient(create_app(settings(), MissingUnderlyingGateway())) as client:
        response = client.get(
            "/v1/options/chain-snapshot",
            params={"symbol": "AAPL", "expiration": "20260918"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["underlying_snapshot"] is None
    assert payload["underlying_attempts"] == 2
    assert payload["retried_contracts"] == 1
    assert payload["reference_underlying_price"] == 200
    assert payload["reference_underlying_price_source"] == "option_model_median"
    assert payload["reference_underlying_price_collected_at"] == "2026-08-14T14:00:00Z"
    assert payload["reference_underlying_price_samples"] == 6
    assert payload["market_quote_complete"] is False
    assert payload["model_complete"] is True


def test_batch_does_not_label_model_only_options_as_market_quotes() -> None:
    with TestClient(create_app(settings(), ModelOnlyOptionGateway())) as client:
        response = client.get(
            "/v1/options/chain-snapshot",
            params={"symbol": "AAPL", "expiration": "20260918"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["underlying_snapshot"] is not None
    assert payload["market_quote_complete"] is False
    assert payload["quote_complete"] is False
    assert payload["model_complete"] is True
    assert payload["complete"] is False
    for quote in payload["quotes"]:
        snapshot = quote["snapshot"]
        assert snapshot["market_quote_complete"] is False
        assert snapshot["quote_complete"] is False
        assert snapshot["model_complete"] is True
        assert (snapshot["bid"], snapshot["ask"], snapshot["last"]) == (None, None, None)


def test_batch_preflight_populates_delayed_route_before_quote_fanout() -> None:
    gateway = EntitlementPreflightGateway()
    with TestClient(create_app(settings(), gateway)) as client:
        response = client.get(
            "/v1/options/chain-snapshot",
            params={"symbol": "AAPL", "expiration": "20260918"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["preflight_attempted"] is True
    assert payload["preflight_market_data_type"] == "delayed"
    assert payload["preflight_seconds"] >= 0
    option_requests = [
        request for request in gateway.market_data_requests if request.contract.sec_type == "OPT"
    ]
    assert option_requests[0].market_data_type is MarketDataType.DELAYED
    assert all(
        request.market_data_type is MarketDataType.DELAYED for request in option_requests[1:]
    )
