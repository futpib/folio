from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from ibkr_tws_http.app import create_app
from ibkr_tws_http.config import ExposureConfig, ServerConfig, Settings
from ibkr_tws_http.gateway import (
    AccountValue,
    ContractInfo,
    ContractSearchResult,
    GatewayRequestError,
    GatewayStatus,
    HistoricalBar,
    OptionChain,
    Position,
)


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


class FakeGateway:
    def __init__(self) -> None:
        self.started = False
        self.summary_tags: frozenset[str] | None = None
        self.last_contract = None

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
        return [contract(), contract("MSFT")]

    async def search_contracts(self, pattern: str) -> list[ContractSearchResult]:
        return [
            ContractSearchResult(contract(), ("OPT", "FUT")),
            ContractSearchResult(contract("MSFT"), ("OPT",)),
        ]

    async def option_chains(self, request) -> list[OptionChain]:
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
        return {"bid": 199.5, "ask": 199.6, "bid_size": 100}

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
                },
                "derivative_security_types": ["OPT"],
            }
        ]

        response = client.post(
            "/v1/options/chains",
            json={"underlying_symbol": "aapl", "underlying_con_id": 265598},
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
            json={"contract": {"symbol": "aapl"}},
        )
        assert response.status_code == 200
        assert response.json()["fields"]["bid"] == 199.5

        response = client.post(
            "/v1/historical-bars",
            json={"contract": {"symbol": "aapl"}},
        )
        assert response.status_code == 200
        assert response.json()[0]["volume"] == "1000"

        openapi = client.get("/openapi.json").json()
        assert set(openapi["paths"]) == {
            "/healthz",
            "/v1/status",
            "/v1/accounts",
            "/v1/account-summary",
            "/v1/positions",
            "/v1/contracts/details",
            "/v1/contracts/search",
            "/v1/options/chains",
            "/v1/market-data/snapshot",
            "/v1/historical-bars",
        }
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

        denied_chain = client.post(
            "/v1/options/chains",
            json={"underlying_symbol": "MSFT", "underlying_con_id": 272093},
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
        response = client.post(
            "/v1/options/chains",
            json={"underlying_symbol": "AAPL", "underlying_con_id": 265598},
        )
    assert response.status_code == 403
    assert response.json() == {"detail": "security type 'OPT' is not allowed"}


class ErrorGateway(FakeGateway):
    async def positions(self) -> list[Position]:
        raise GatewayRequestError("account DU111 is invalid")


def test_upstream_error_details_are_not_returned() -> None:
    with TestClient(create_app(settings(), ErrorGateway())) as client:
        response = client.get("/v1/positions")
    assert response.status_code == 502
    assert response.json() == {"detail": "TWS rejected the request"}
    assert "DU111" not in response.text
