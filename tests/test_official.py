from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import ClassVar

import pytest

from ibkr_tws_http.config import TwsConfig
from ibkr_tws_http.gateway import GatewayRequestError, GatewayTimeout, TwsNotice
from ibkr_tws_http.models import (
    ContractSpec,
    HistoricalBarsRequest,
    MarketDataSnapshotRequest,
    MarketDataType,
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
        return {
            0: "BID_SIZE",
            1: "BID",
            2: "ASK",
            3: "ASK_SIZE",
            4: "LAST",
            5: "LAST_SIZE",
            8: "VOLUME",
            13: "MODEL_OPTION",
            27: "OPTION_CALL_OPEN_INTEREST",
            29: "OPTION_CALL_VOLUME",
            37: "MARK_PRICE",
            45: "LAST_TIMESTAMP",
            66: "DELAYED_BID",
            67: "DELAYED_ASK",
            74: "DELAYED_VOLUME",
            83: "DELAYED_MODEL_OPTION",
            88: "DELAYED_LAST_TIMESTAMP",
        }.get(value, str(value))


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
        self.market_data_type = 1
        self.market_data_type_requests: list[int] = []
        self.last_generic_ticks = ""
        self.market_data_requests: list[tuple[str, str, bool]] = []
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
        resolved = apple_contract()
        if requested.secType == "OPT":
            resolved.conId = 829402031
            resolved.secType = "OPT"
            resolved.lastTradeDateOrContractMonth = requested.lastTradeDateOrContractMonth
            resolved.strike = requested.strike
            resolved.right = requested.right
            resolved.multiplier = "100"
            resolved.localSymbol = "AAPL  260918C00200000"
            resolved.tradingClass = "AAPL"
        details = SimpleNamespace(contract=resolved, longName="Apple Inc.", minTick=0.01)
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
        self.last_generic_ticks = generic_ticks
        self.market_data_requests.append((requested.secType, generic_ticks, snapshot))
        self.wrapper.marketDataType(request_id, self.market_data_type)
        self.wrapper.tickPrice(request_id, 1, 199.5, None)
        self.wrapper.tickPrice(request_id, 2, 199.6, None)
        self.wrapper.tickSize(request_id, 0, 100)
        self.wrapper.tickSize(request_id, 3, 120)
        self.wrapper.tickPrice(request_id, 4, 199.55, None)
        self.wrapper.tickSize(request_id, 5, 5)
        self.wrapper.tickSize(request_id, 8, 1000)
        self.wrapper.tickString(request_id, 45, "1786718400")
        if requested.secType == "OPT":
            self.wrapper.tickSize(request_id, 27, 2500)
            self.wrapper.tickSize(request_id, 29, 125)
            self.wrapper.tickPrice(request_id, 37, 2.05, None)
            self.wrapper.tickOptionComputation(
                request_id,
                13,
                0,
                0.75,
                0.55,
                2.05,
                0.0,
                0.08,
                0.02,
                -0.01,
                5.0,
            )
        if snapshot:
            self.wrapper.tickSnapshotEnd(request_id)

    def reqMarketDataType(self, market_data_type: int) -> None:
        self.market_data_type = market_data_type
        self.market_data_type_requests.append(market_data_type)

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

        option_details = await gateway.contract_details(
            ContractSpec(
                symbol="AAPL",
                sec_type="OPT",
                last_trade_date_or_contract_month="20260918",
                strike=200,
                right="C",
            )
        )
        assert option_details[0].last_trade_date_or_contract_month == "20260918"
        assert option_details[0].strike == 200
        assert option_details[0].right == "C"
        assert option_details[0].multiplier == "100"

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
        assert snapshot.market_data_type is MarketDataType.LIVE
        assert snapshot.fields["bid"] == 199.5
        assert snapshot.fields["ask_size"] == 120

        option_snapshot = await gateway.market_data_snapshot(
            MarketDataSnapshotRequest(
                contract=ContractSpec(
                    symbol="AAPL",
                    sec_type="OPT",
                    last_trade_date_or_contract_month="20260918",
                    strike=200,
                    right="C",
                )
            )
        )
        assert option_snapshot.fields["option_call_open_interest"] == 2500
        assert option_snapshot.option_computations["model"].delta == 0.55
        assert option_snapshot.quote_complete is True
        assert option_snapshot.enrichment_complete is True
        assert option_snapshot.complete is True
        assert FakeClient.instances[-1].market_data_requests[-1] == (
            "OPT",
            "100,101,106,221",
            False,
        )

        delayed_option_snapshot = await gateway.market_data_snapshot(
            MarketDataSnapshotRequest(
                contract=ContractSpec(
                    symbol="AAPL",
                    sec_type="OPT",
                    last_trade_date_or_contract_month="20260918",
                    strike=200,
                    right="C",
                ),
                market_data_type=MarketDataType.DELAYED,
                generic_ticks=[100, 101, 233],
            )
        )
        assert delayed_option_snapshot.market_data_type is MarketDataType.DELAYED
        assert delayed_option_snapshot.quote_complete is True
        assert delayed_option_snapshot.enrichment_complete is False
        assert delayed_option_snapshot.complete is False
        assert FakeClient.instances[-1].last_generic_ticks == ""
        assert FakeClient.instances[-1].market_data_requests[-1] == (
            "OPT",
            "",
            True,
        )

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
            with pytest.raises(GatewayRequestError) as raised:
                await gateway.contract_details(ContractSpec(symbol="AAPL"))
            assert raised.value.code in {200, 201}
            assert raised.value.public_message.endswith("callback error")
            assert gateway._pending == {}
        finally:
            await gateway.stop()

    asyncio.run(scenario())


class DelayedFallbackClient(FakeClient):
    def reqMktData(
        self,
        request_id: int,
        requested: FakeContract,
        generic_ticks: str,
        snapshot: bool,
        regulatory_snapshot: bool,
        options: list[object],
    ) -> None:
        if self.market_data_type == 1:
            self.wrapper.error(request_id, 354, "Not subscribed to requested market data", "")
            return
        self.wrapper.marketDataType(request_id, 3)
        self.wrapper.tickPrice(request_id, 66, 198.5, None)
        self.wrapper.tickPrice(request_id, 67, 198.7, None)
        self.wrapper.tickSize(request_id, 74, 900)
        self.wrapper.tickString(request_id, 88, "1786714800")
        self.wrapper.tickSnapshotEnd(request_id)


class EmptyLiveFallbackClient(DelayedFallbackClient):
    def reqMktData(
        self,
        request_id: int,
        requested: FakeContract,
        generic_ticks: str,
        snapshot: bool,
        regulatory_snapshot: bool,
        options: list[object],
    ) -> None:
        if self.market_data_type == 1:
            self.wrapper.tickSnapshotEnd(request_id)
            return
        super().reqMktData(
            request_id,
            requested,
            generic_ticks,
            snapshot,
            regulatory_snapshot,
            options,
        )


class ApiSubscriptionFallbackClient(DelayedFallbackClient):
    def reqMktData(
        self,
        request_id: int,
        requested: FakeContract,
        generic_ticks: str,
        snapshot: bool,
        regulatory_snapshot: bool,
        options: list[object],
    ) -> None:
        if self.market_data_type == 1:
            self.wrapper.error(
                request_id,
                10089,
                "Requested market data requires additional subscription for API",
                "",
            )
            return
        super().reqMktData(
            request_id,
            requested,
            generic_ticks,
            snapshot,
            regulatory_snapshot,
            options,
        )


class DelayedNotEnabledFallbackClient(ApiSubscriptionFallbackClient):
    def reqMktData(
        self,
        request_id: int,
        requested: FakeContract,
        generic_ticks: str,
        snapshot: bool,
        regulatory_snapshot: bool,
        options: list[object],
    ) -> None:
        if self.market_data_type == 1:
            self.wrapper.error(
                request_id,
                10168,
                "Requested market data is not subscribed. Delayed market data is not enabled",
                "",
            )
            return
        super().reqMktData(
            request_id,
            requested,
            generic_ticks,
            snapshot,
            regulatory_snapshot,
            options,
        )


class ClosedMarketFallbackClient(DelayedFallbackClient):
    def reqMktData(
        self,
        request_id: int,
        requested: FakeContract,
        generic_ticks: str,
        snapshot: bool,
        regulatory_snapshot: bool,
        options: list[object],
    ) -> None:
        if self.market_data_type in {1, 3}:
            self.wrapper.tickPrice(request_id, 1 if self.market_data_type == 1 else 66, -1, None)
            self.wrapper.tickSnapshotEnd(request_id)
            return
        self.wrapper.marketDataType(request_id, 4)
        self.wrapper.tickPrice(request_id, 66, 198.5, None)
        self.wrapper.tickPrice(request_id, 67, 198.7, None)
        self.wrapper.tickString(request_id, 88, "1786714800")
        self.wrapper.tickSnapshotEnd(request_id)


class PartialSubscriptionClient(FakeClient):
    def reqMktData(
        self,
        request_id: int,
        requested: FakeContract,
        generic_ticks: str,
        snapshot: bool,
        regulatory_snapshot: bool,
        options: list[object],
    ) -> None:
        self.wrapper.error(
            request_id,
            10090,
            "Part of requested market data for DU111 is not subscribed",
            "",
        )
        super().reqMktData(
            request_id,
            requested,
            generic_ticks,
            snapshot,
            regulatory_snapshot,
            options,
        )


class CleanDelayedOptionFallbackClient(FakeClient):
    live_error_code = 354

    def reqMktData(
        self,
        request_id: int,
        requested: FakeContract,
        generic_ticks: str,
        snapshot: bool,
        regulatory_snapshot: bool,
        options: list[object],
    ) -> None:
        self.last_generic_ticks = generic_ticks
        self.market_data_requests.append((requested.secType, generic_ticks, snapshot))
        if self.market_data_type == 1:
            self.wrapper.error(
                request_id,
                self.live_error_code,
                "Live market data is not available; delayed fallback is required",
                "",
            )
            return

        self.wrapper.error(
            request_id,
            2187,
            "Generic tick data is not available when using delayed market data fallback",
            "",
        )
        self.wrapper.marketDataType(request_id, 3)
        self.wrapper.tickPrice(request_id, 66, 0.75, None)
        self.wrapper.tickPrice(request_id, 67, 1.05, None)
        self.wrapper.tickOptionComputation(
            request_id,
            83,
            0,
            0.857,
            0.447,
            0.9,
            0.0,
            0.08,
            0.02,
            -0.01,
            4.8567,
        )
        self.wrapper.tickSnapshotEnd(request_id)


@pytest.mark.parametrize(
    "client_type",
    [
        DelayedFallbackClient,
        ApiSubscriptionFallbackClient,
        DelayedNotEnabledFallbackClient,
        EmptyLiveFallbackClient,
    ],
)
def test_auto_market_data_falls_back_to_delayed_quotes(client_type) -> None:
    async def scenario() -> None:
        client_type.instances.clear()
        gateway = OfficialGateway(
            TwsConfig(connect_timeout_seconds=0.5, request_timeout_seconds=0.5),
            IbApiBindings(client_type, FakeWrapper, FakeContract, FakeTickType),
        )
        await gateway.start()
        try:
            snapshot = await gateway.market_data_snapshot(
                MarketDataSnapshotRequest(contract=ContractSpec(symbol="AAPL"))
            )
            assert snapshot.market_data_type is MarketDataType.DELAYED
            assert snapshot.fields == {
                "delayed_bid": 198.5,
                "delayed_ask": 198.7,
                "delayed_volume": 900,
                "delayed_last_timestamp": "1786714800",
            }
            assert client_type.instances[-1].market_data_type_requests == [1, 3]
        finally:
            await gateway.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("live_error_code", [354, 10091, 10167, 10186, 2186, 2187])
def test_auto_option_quotes_retry_delayed_without_generic_ticks(live_error_code: int) -> None:
    async def scenario() -> None:
        CleanDelayedOptionFallbackClient.instances.clear()
        CleanDelayedOptionFallbackClient.live_error_code = live_error_code
        gateway = OfficialGateway(
            TwsConfig(connect_timeout_seconds=0.5, request_timeout_seconds=0.5),
            IbApiBindings(
                CleanDelayedOptionFallbackClient,
                FakeWrapper,
                FakeContract,
                FakeTickType,
            ),
        )
        await gateway.start()
        try:
            snapshot = await gateway.market_data_snapshot(
                MarketDataSnapshotRequest(
                    contract=ContractSpec(
                        symbol="XXI",
                        sec_type="OPT",
                        last_trade_date_or_contract_month="20280121",
                        strike=10,
                        right="C",
                    )
                )
            )
            assert snapshot.market_data_type is MarketDataType.DELAYED
            assert snapshot.quote_complete is True
            assert snapshot.enrichment_complete is False
            assert snapshot.complete is False
            assert snapshot.fields["delayed_bid"] == 0.75
            assert snapshot.option_computations["model"].delta == 0.447
            assert snapshot.notices == (
                TwsNotice(
                    2187,
                    "Generic tick data is not available when using delayed market data fallback",
                ),
            )
            client = CleanDelayedOptionFallbackClient.instances[-1]
            assert client.market_data_requests == [
                ("OPT", "100,101,106,221", False),
                ("OPT", "", True),
            ]
            assert client.market_data_type_requests == [1, 3]
        finally:
            await gateway.stop()

    asyncio.run(scenario())


def test_auto_underlying_quote_retries_after_2186() -> None:
    async def scenario() -> None:
        CleanDelayedOptionFallbackClient.instances.clear()
        CleanDelayedOptionFallbackClient.live_error_code = 2186
        gateway = OfficialGateway(
            TwsConfig(connect_timeout_seconds=0.5, request_timeout_seconds=0.5),
            IbApiBindings(
                CleanDelayedOptionFallbackClient,
                FakeWrapper,
                FakeContract,
                FakeTickType,
            ),
        )
        await gateway.start()
        try:
            snapshot = await gateway.market_data_snapshot(
                MarketDataSnapshotRequest(contract=ContractSpec(symbol="XXI"))
            )
            assert snapshot.market_data_type is MarketDataType.DELAYED
            assert snapshot.quote_complete is True
            assert snapshot.enrichment_complete is True
            assert snapshot.complete is True
            client = CleanDelayedOptionFallbackClient.instances[-1]
            assert client.market_data_requests == [
                ("STK", "", True),
                ("STK", "", True),
            ]
        finally:
            await gateway.stop()

    asyncio.run(scenario())


class EmptyMarketDataClient(FakeClient):
    def reqMktData(
        self,
        request_id: int,
        requested: FakeContract,
        generic_ticks: str,
        snapshot: bool,
        regulatory_snapshot: bool,
        options: list[object],
    ) -> None:
        self.wrapper.tickSnapshotEnd(request_id)


class PartialStreamingMarketDataClient(FakeClient):
    def reqMktData(
        self,
        request_id: int,
        requested: FakeContract,
        generic_ticks: str,
        snapshot: bool,
        regulatory_snapshot: bool,
        options: list[object],
    ) -> None:
        assert requested.secType == "OPT"
        assert snapshot is False
        self.wrapper.tickSize(request_id, 27, 2500)


def test_empty_market_data_is_a_timeout_instead_of_a_clean_snapshot() -> None:
    async def scenario() -> None:
        gateway = OfficialGateway(
            TwsConfig(connect_timeout_seconds=0.1, request_timeout_seconds=0.1),
            IbApiBindings(EmptyMarketDataClient, FakeWrapper, FakeContract, FakeTickType),
        )
        await gateway.start()
        try:
            with pytest.raises(GatewayTimeout, match="returned no market data"):
                await gateway.market_data_snapshot(
                    MarketDataSnapshotRequest(
                        contract=ContractSpec(symbol="AAPL"),
                        market_data_type=MarketDataType.DELAYED,
                    )
                )
        finally:
            await gateway.stop()

    asyncio.run(scenario())


def test_streaming_market_data_returns_observable_partial_result_at_deadline() -> None:
    async def scenario() -> None:
        gateway = OfficialGateway(
            TwsConfig(connect_timeout_seconds=0.1, request_timeout_seconds=0.1),
            IbApiBindings(
                PartialStreamingMarketDataClient,
                FakeWrapper,
                FakeContract,
                FakeTickType,
            ),
        )
        await gateway.start()
        try:
            snapshot = await gateway.market_data_snapshot(
                MarketDataSnapshotRequest(
                    contract=ContractSpec(symbol="AAPL", sec_type="OPT"),
                    market_data_type=MarketDataType.LIVE,
                )
            )
            assert snapshot.quote_complete is False
            assert snapshot.enrichment_complete is False
            assert snapshot.complete is False
            assert snapshot.fields["option_call_open_interest"] == 2500
            assert snapshot.notices[-1] == TwsNotice(
                None,
                "market data collection reached its deadline; partial data returned",
            )
        finally:
            await gateway.stop()

    asyncio.run(scenario())


def test_partial_market_data_notice_keeps_ticks_and_is_redacted() -> None:
    async def scenario() -> None:
        PartialSubscriptionClient.instances.clear()
        gateway = OfficialGateway(
            TwsConfig(connect_timeout_seconds=0.5, request_timeout_seconds=0.5),
            IbApiBindings(PartialSubscriptionClient, FakeWrapper, FakeContract, FakeTickType),
        )
        await gateway.start()
        try:
            snapshot = await gateway.market_data_snapshot(
                MarketDataSnapshotRequest(
                    contract=ContractSpec(symbol="AAPL"),
                    market_data_type=MarketDataType.LIVE,
                )
            )
            assert snapshot.fields["bid"] == 199.5
            assert snapshot.notices == (
                TwsNotice(
                    10090,
                    "Part of requested market data for [redacted account] is not subscribed",
                ),
            )
        finally:
            await gateway.stop()

    asyncio.run(scenario())


def test_auto_market_data_reaches_delayed_frozen_when_market_is_closed() -> None:
    async def scenario() -> None:
        ClosedMarketFallbackClient.instances.clear()
        gateway = OfficialGateway(
            TwsConfig(connect_timeout_seconds=0.5, request_timeout_seconds=0.5),
            IbApiBindings(ClosedMarketFallbackClient, FakeWrapper, FakeContract, FakeTickType),
        )
        await gateway.start()
        try:
            snapshot = await gateway.market_data_snapshot(
                MarketDataSnapshotRequest(contract=ContractSpec(symbol="AAPL"))
            )
            assert snapshot.market_data_type is MarketDataType.DELAYED_FROZEN
            assert snapshot.fields["delayed_bid"] == 198.5
            assert ClosedMarketFallbackClient.instances[-1].market_data_type_requests == [1, 3, 4]
        finally:
            await gateway.stop()

    asyncio.run(scenario())
