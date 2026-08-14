from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import ClassVar

import pytest

from ibkr_tws_http.config import TwsConfig
from ibkr_tws_http.gateway import GatewayRequestError
from ibkr_tws_http.models import (
    ContractSpec,
    HistoricalBarsRequest,
    MarketDataSnapshotRequest,
    OptionChainsRequest,
)
from ibkr_tws_http.official import IbApiBindings, OfficialGateway, load_ibapi


class FakeWrapper:
    def __init__(self) -> None:
        pass


class FakeContract:
    pass


class FakeTickType:
    @staticmethod
    def toStr(value: int) -> str:
        return {1: "BID", 2: "ASK", 0: "BID_SIZE"}.get(value, str(value))


def apple_contract() -> FakeContract:
    value = FakeContract()
    value.conId = 265598
    value.symbol = "AAPL"
    value.secType = "STK"
    value.exchange = "SMART"
    value.primaryExchange = "NASDAQ"
    value.currency = "USD"
    value.localSymbol = "AAPL"
    value.tradingClass = "NMS"
    value.description = "Apple Inc."
    return value


class FakeClient:
    instances: ClassVar[list[FakeClient]] = []

    def __init__(self, wrapper: FakeWrapper) -> None:
        self.wrapper = wrapper
        self.finished = threading.Event()
        self.cancelled: list[tuple[str, int | None]] = []
        self.last_historical_end = None
        type(self).instances.append(self)

    def connect(self, host: str, port: int, clientId: int) -> bool:
        self.wrapper.nextValidId(100)
        self.wrapper.managedAccounts("DU111,DU222")
        return True

    def run(self) -> None:
        self.finished.wait()

    def disconnect(self) -> None:
        self.finished.set()

    def serverVersion(self) -> int:
        return 204

    def twsConnectionTime(self) -> str:
        return "20260814 12:00:00 UTC"

    def reqAccountSummary(self, request_id: int, group: str, tags: str) -> None:
        self.wrapper.accountSummary(request_id, "DU111", "NetLiquidation", "10000", "USD")
        self.wrapper.accountSummaryEnd(request_id)

    def cancelAccountSummary(self, request_id: int) -> None:
        self.cancelled.append(("account-summary", request_id))

    def reqPositions(self) -> None:
        self.wrapper.position("DU111", apple_contract(), "2.5", 180.0)
        self.wrapper.positionEnd()

    def cancelPositions(self) -> None:
        self.cancelled.append(("positions", None))

    def reqContractDetails(self, request_id: int, requested: FakeContract) -> None:
        details = SimpleNamespace(contract=apple_contract(), longName="Apple Inc.", minTick=0.01)
        self.wrapper.contractDetails(request_id, details)
        self.wrapper.contractDetailsEnd(request_id)

    def reqMatchingSymbols(self, request_id: int, pattern: str) -> None:
        description = SimpleNamespace(contract=apple_contract(), derivativeSecTypes=["OPT", "WAR"])
        self.wrapper.symbolSamples(request_id, [description])

    def reqSecDefOptParams(
        self,
        request_id: int,
        underlying_symbol: str,
        exchange: str,
        underlying_security_type: str,
        underlying_con_id: int,
    ) -> None:
        self.wrapper.securityDefinitionOptionParameter(
            request_id,
            "SMART",
            underlying_con_id,
            underlying_symbol,
            "100",
            {"20260925", "20260918"},
            {205.0, 195.0, 200.0},
        )
        self.wrapper.securityDefinitionOptionParameterEnd(request_id)

    def reqMktData(
        self,
        request_id: int,
        requested: FakeContract,
        generic_ticks: str,
        snapshot: bool,
        regulatory_snapshot: bool,
        options: list[object],
    ) -> None:
        self.wrapper.tickPrice(request_id, 1, 199.5, None)
        self.wrapper.tickPrice(request_id, 2, 199.6, None)
        self.wrapper.tickSize(request_id, 0, 100)
        self.wrapper.tickSnapshotEnd(request_id)

    def cancelMktData(self, request_id: int) -> None:
        self.cancelled.append(("market-data", request_id))

    def reqHistoricalData(
        self,
        request_id: int,
        requested: FakeContract,
        end: str,
        duration: str,
        bar_size: str,
        data_type: str,
        use_rth: int,
        format_date: int,
        keep_up_to_date: bool,
        options: list[object],
    ) -> None:
        self.last_historical_end = end
        bar = SimpleNamespace(
            date="20260814 12:00:00",
            open=198,
            high=200,
            low=197.5,
            close=199.5,
            volume="1000",
            wap="199.2",
            barCount=75,
        )
        self.wrapper.historicalData(request_id, bar)
        self.wrapper.historicalDataEnd(request_id, "", "")

    def cancelHistoricalData(self, request_id: int) -> None:
        self.cancelled.append(("historical", request_id))


def test_loads_vendored_official_client() -> None:
    import ibapi

    bindings = load_ibapi()
    assert ibapi.__version__ == "10.49.02"
    assert bindings.client.__module__ == "ibapi.client"
    assert bindings.wrapper.__module__ == "ibapi.wrapper"


def test_official_callback_bridge_round_trip() -> None:
    async def scenario() -> None:
        FakeClient.instances.clear()
        gateway = OfficialGateway(
            TwsConfig(connect_timeout_seconds=0.5, request_timeout_seconds=0.5),
            IbApiBindings(FakeClient, FakeWrapper, FakeContract, FakeTickType),
        )
        await gateway.start()
        assert await gateway.accounts() == ["DU111", "DU222"]
        assert gateway.status().server_version == 204

        summary = await gateway.account_summary(frozenset({"NetLiquidation"}))
        assert summary[0].value == "10000"

        positions = await gateway.positions()
        assert str(positions[0].position) == "2.5"
        assert positions[0].contract.symbol == "AAPL"

        details = await gateway.contract_details(ContractSpec(symbol="AAPL"))
        assert details[0].long_name == "Apple Inc."

        search = await gateway.search_contracts("apple")
        assert search[0].contract.symbol == "AAPL"
        assert search[0].contract.long_name == "Apple Inc."
        assert search[0].derivative_security_types == ("OPT", "WAR")

        chains = await gateway.option_chains(
            OptionChainsRequest(underlying_symbol="AAPL", underlying_con_id=265598)
        )
        assert chains[0].exchange == "SMART"
        assert chains[0].expirations == ("20260918", "20260925")
        assert chains[0].strikes == (195.0, 200.0, 205.0)

        snapshot = await gateway.market_data_snapshot(
            MarketDataSnapshotRequest(contract=ContractSpec(symbol="AAPL"))
        )
        assert snapshot == {"bid": 199.5, "ask": 199.6, "bid_size": 100}

        bars = await gateway.historical_bars(
            HistoricalBarsRequest(
                contract=ContractSpec(symbol="AAPL"),
                end="2026-08-14T15:00:00+03:00",
            )
        )
        assert bars[0].close == 199.5
        client = FakeClient.instances[-1]
        assert client.last_historical_end == "20260814 12:00:00 UTC"
        assert {name for name, request_id in client.cancelled} == {
            "account-summary",
            "positions",
            "market-data",
            "historical",
        }
        await gateway.stop()
        assert not gateway.status().connected

    asyncio.run(scenario())


class OldErrorClient(FakeClient):
    def reqContractDetails(self, request_id: int, requested: FakeContract) -> None:
        self.wrapper.error(request_id, 200, "old callback error", "")


class NewErrorClient(FakeClient):
    def reqContractDetails(self, request_id: int, requested: FakeContract) -> None:
        self.wrapper.error(request_id, 1786718400, 201, "new callback error", "")


@pytest.mark.parametrize("client_type", [OldErrorClient, NewErrorClient])
def test_both_official_error_callback_signatures_fail_request(client_type) -> None:
    async def scenario() -> None:
        gateway = OfficialGateway(
            TwsConfig(connect_timeout_seconds=0.5, request_timeout_seconds=0.5),
            IbApiBindings(client_type, FakeWrapper, FakeContract, FakeTickType),
        )
        await gateway.start()
        try:
            await gateway.accounts()
            with pytest.raises(GatewayRequestError):
                await gateway.contract_details(ContractSpec(symbol="AAPL"))
            assert gateway._pending == {}
        finally:
            await gateway.stop()

    asyncio.run(scenario())
