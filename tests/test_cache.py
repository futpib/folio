from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from ibkr_tws_http.cache import AsyncTtlLruCache, CachingGateway
from ibkr_tws_http.gateway import GatewayStatus, GatewayTimeout, MarketDataSnapshot, TwsNotice
from ibkr_tws_http.models import (
    ContractSpec,
    HistoricalBarsRequest,
    MarketDataSnapshotRequest,
    MarketDataType,
    OptionChainsRequest,
)


class CountingGateway:
    def __init__(self) -> None:
        self.connection_time = "session-1"
        self.calls: dict[str, int] = {}

    async def start(self) -> None:
        self._called("start")

    async def stop(self) -> None:
        self._called("stop")

    def status(self) -> GatewayStatus:
        return GatewayStatus(True, 225, self.connection_time)

    async def accounts(self):
        self._called("accounts")
        return []

    async def account_summary(self, tags):
        self._called("account_summary")
        return []

    async def positions(self):
        self._called("positions")
        return []

    async def contract_details(self, contract):
        self._called("contract_details")
        return []

    async def search_contracts(self, pattern):
        self._called("search_contracts")
        return []

    async def option_chains(self, request):
        self._called("option_chains")
        return []

    async def market_data_snapshot(self, request):
        self._called("market_data_snapshot")
        return MarketDataSnapshot(
            market_data_type=MarketDataType.DELAYED,
            as_of=datetime.now(UTC),
            market_quote_complete=True,
            model_complete=True,
            enrichment_complete=True,
            complete=True,
            fields={"delayed_bid": 199.5},
            option_computations={},
        )

    async def historical_bars(self, request):
        self._called("historical_bars")
        return []

    def _called(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1


class DelayedRouteGateway(CountingGateway):
    def __init__(self) -> None:
        super().__init__()
        self.requested_market_data_types: list[MarketDataType] = []
        self.fail_delayed = False

    async def market_data_snapshot(self, request):
        self._called("market_data_snapshot")
        self.requested_market_data_types.append(request.market_data_type)
        if self.fail_delayed and request.market_data_type is MarketDataType.DELAYED:
            raise GatewayTimeout("cached route stopped working")
        return MarketDataSnapshot(
            market_data_type=MarketDataType.DELAYED,
            as_of=datetime.now(UTC),
            market_quote_complete=True,
            model_complete=True,
            enrichment_complete=True,
            complete=True,
            fields={"delayed_bid": 199.5},
            option_computations={},
            notices=(TwsNotice(2186, "using delayed data"),),
        )


class BlockingQuoteGateway(CountingGateway):
    def __init__(self) -> None:
        super().__init__()
        self.quote_started = asyncio.Event()
        self.quote_cancelled = asyncio.Event()

    async def market_data_snapshot(self, request):
        self.quote_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.quote_cancelled.set()


def test_ttl_lru_cache_expires_and_evicts_least_recently_used() -> None:
    async def scenario() -> None:
        now = [0.0]
        cache = AsyncTtlLruCache[str, int](
            max_entries=2,
            ttl_seconds=10,
            clock=lambda: now[0],
        )
        await cache.set("a", 1)
        await cache.set("b", 2)
        assert await cache.lookup("a") == (True, 1)

        await cache.set("c", 3)
        assert await cache.lookup("b") == (False, None)
        assert await cache.lookup("a") == (True, 1)
        assert await cache.lookup("c") == (True, 3)

        now[0] = 10
        assert await cache.lookup("a") == (False, None)
        assert await cache.size() == 0

    asyncio.run(scenario())


def test_ttl_lru_cache_single_flights_concurrent_loaders() -> None:
    async def scenario() -> None:
        cache = AsyncTtlLruCache[str, int](max_entries=2, ttl_seconds=10)
        release = asyncio.Event()
        calls = 0

        async def load() -> int:
            nonlocal calls
            calls += 1
            await release.wait()
            return 7

        first = asyncio.create_task(cache.get_or_load("same", load))
        second = asyncio.create_task(cache.get_or_load("same", load))
        await asyncio.sleep(0)
        release.set()

        assert await asyncio.gather(first, second) == [7, 7]
        assert calls == 1
        assert await cache.get_or_load("same", load) == 7
        assert calls == 1

    asyncio.run(scenario())


def test_gateway_caches_metadata_and_quotes_until_tws_connection_changes() -> None:
    async def scenario() -> None:
        backend = CountingGateway()
        gateway = CachingGateway(backend)
        contract = ContractSpec(symbol="AAPL")
        chains = OptionChainsRequest(underlying_symbol="AAPL", underlying_con_id=265598)
        quote = MarketDataSnapshotRequest(contract=contract)

        await gateway.start()
        assert await gateway.search_contracts("aapl") == []
        assert await gateway.search_contracts("AAPL") == []
        assert await gateway.contract_details(contract) == []
        assert await gateway.contract_details(contract) == []
        assert await gateway.option_chains(chains) == []
        assert await gateway.option_chains(chains) == []
        first_quote = await gateway.market_data_snapshot(quote)
        assert await gateway.market_data_snapshot(quote) is first_quote
        assert backend.calls == {
            "start": 1,
            "search_contracts": 1,
            "contract_details": 1,
            "option_chains": 1,
            "market_data_snapshot": 1,
        }

        backend.connection_time = "session-2"
        assert await gateway.search_contracts("AAPL") == []
        await gateway.market_data_snapshot(quote)
        assert backend.calls["search_contracts"] == 2
        assert backend.calls["market_data_snapshot"] == 2

        await gateway.stop()
        assert backend.calls["stop"] == 1

    asyncio.run(scenario())


def test_gateway_does_not_cache_uncached_operations() -> None:
    async def scenario() -> None:
        backend = CountingGateway()
        gateway = CachingGateway(backend)
        await gateway.start()

        assert await gateway.accounts() == []
        assert await gateway.accounts() == []
        assert await gateway.account_summary(frozenset({"NetLiquidation"})) == []
        assert await gateway.positions() == []
        assert (
            await gateway.historical_bars(
                HistoricalBarsRequest(contract=ContractSpec(symbol="AAPL"))
            )
            == []
        )

        assert backend.calls["accounts"] == 2
        assert backend.calls["account_summary"] == 1
        assert backend.calls["positions"] == 1
        assert backend.calls["historical_bars"] == 1
        await gateway.stop()

    asyncio.run(scenario())


def test_gateway_lru_remembers_delayed_route_beyond_quote_microcache() -> None:
    async def scenario() -> None:
        backend = DelayedRouteGateway()
        gateway = CachingGateway(backend, quote_ttl_seconds=0.01)
        request = MarketDataSnapshotRequest(
            contract=ContractSpec(symbol="AAPL", sec_type="OPT", strike=200, right="C")
        )
        await gateway.start()

        await gateway.market_data_snapshot(request)
        await asyncio.sleep(0.02)
        await gateway.market_data_snapshot(request)

        assert backend.requested_market_data_types == [
            MarketDataType.AUTO,
            MarketDataType.DELAYED,
        ]
        await gateway.stop()

    asyncio.run(scenario())


def test_gateway_quote_cache_does_not_shield_tws_collection_from_cancellation() -> None:
    async def scenario() -> None:
        backend = BlockingQuoteGateway()
        gateway = CachingGateway(backend)
        await gateway.start()
        task = asyncio.create_task(
            gateway.market_data_snapshot(
                MarketDataSnapshotRequest(contract=ContractSpec(symbol="AAPL"))
            )
        )
        await backend.quote_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert backend.quote_cancelled.is_set()
        await gateway.stop()

    asyncio.run(scenario())


def test_gateway_retries_auto_when_cached_delayed_route_stops_working() -> None:
    async def scenario() -> None:
        backend = DelayedRouteGateway()
        gateway = CachingGateway(backend, quote_ttl_seconds=0.01)
        request = MarketDataSnapshotRequest(
            contract=ContractSpec(symbol="AAPL", sec_type="OPT", strike=200, right="C")
        )
        await gateway.start()

        await gateway.market_data_snapshot(request)
        await asyncio.sleep(0.02)
        backend.fail_delayed = True
        await gateway.market_data_snapshot(request)

        assert backend.requested_market_data_types == [
            MarketDataType.AUTO,
            MarketDataType.DELAYED,
            MarketDataType.AUTO,
        ]
        await gateway.stop()

    asyncio.run(scenario())


def test_gateway_does_not_replace_implicit_delayed_stock_route() -> None:
    async def scenario() -> None:
        backend = DelayedRouteGateway()
        gateway = CachingGateway(backend, quote_ttl_seconds=0.01)
        request = MarketDataSnapshotRequest(contract=ContractSpec(symbol="AAPL"))
        await gateway.start()

        await gateway.market_data_snapshot(request)
        await asyncio.sleep(0.02)
        await gateway.market_data_snapshot(request)

        assert backend.requested_market_data_types == [
            MarketDataType.AUTO,
            MarketDataType.AUTO,
        ]
        await gateway.stop()

    asyncio.run(scenario())
