from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Generic, TypeVar, cast

from .gateway import (
    AccountValue,
    ContractInfo,
    ContractSearchResult,
    Gateway,
    GatewayRequestError,
    GatewayStatus,
    GatewayTimeout,
    HistoricalBar,
    MarketDataSnapshot,
    OptionChain,
    Position,
)
from .models import (
    ContractSpec,
    HistoricalBarsRequest,
    MarketDataSnapshotRequest,
    MarketDataType,
    OptionChainsRequest,
)

K = TypeVar("K")
V = TypeVar("V")


@dataclass(frozen=True, slots=True)
class _Entry(Generic[V]):
    value: V
    expires_at: float


class AsyncTtlLruCache(Generic[K, V]):
    def __init__(
        self,
        *,
        max_entries: int,
        ttl_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._entries: OrderedDict[K, _Entry[V]] = OrderedDict()
        self._inflight: dict[K, asyncio.Task[V]] = {}
        self._generation = 0
        self._lock = asyncio.Lock()

    async def lookup(self, key: K) -> tuple[bool, V | None]:
        async with self._lock:
            entry = self._lookup_locked(key)
            return (entry is not None, entry.value if entry is not None else None)

    async def set(self, key: K, value: V) -> None:
        async with self._lock:
            self._store_locked(key, value)

    async def delete(self, key: K) -> None:
        async with self._lock:
            self._entries.pop(key, None)

    async def get_or_load(self, key: K, loader: Callable[[], Awaitable[V]]) -> V:
        async with self._lock:
            entry = self._lookup_locked(key)
            if entry is not None:
                return entry.value
            task = self._inflight.get(key)
            if task is None:
                generation = self._generation
                task = asyncio.create_task(self._load(key, generation, loader))
                self._inflight[key] = task
        return await asyncio.shield(task)

    async def clear(self) -> None:
        async with self._lock:
            self._entries.clear()
            self._inflight.clear()
            self._generation += 1

    async def size(self) -> int:
        async with self._lock:
            now = self._clock()
            expired = [key for key, entry in self._entries.items() if entry.expires_at <= now]
            for key in expired:
                del self._entries[key]
            return len(self._entries)

    async def _load(
        self,
        key: K,
        generation: int,
        loader: Callable[[], Awaitable[V]],
    ) -> V:
        current = cast(asyncio.Task[V], asyncio.current_task())
        try:
            value = await loader()
            async with self._lock:
                if generation == self._generation:
                    self._store_locked(key, value)
            return value
        finally:
            async with self._lock:
                if self._inflight.get(key) is current:
                    del self._inflight[key]

    def _lookup_locked(self, key: K) -> _Entry[V] | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expires_at <= self._clock():
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return entry

    def _store_locked(self, key: K, value: V) -> None:
        self._entries[key] = _Entry(value, self._clock() + self._ttl_seconds)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)


class CachingGateway:
    _DELAYED_ROUTE_NOTICE_CODES = frozenset(
        {354, 10089, 10091, 10167, 10168, 10186, 10197, 2186, 2187}
    )

    def __init__(
        self,
        gateway: Gateway,
        *,
        metadata_max_entries: int = 512,
        metadata_ttl_seconds: float = 300,
        quote_max_entries: int = 512,
        quote_ttl_seconds: float = 1,
    ) -> None:
        self._gateway = gateway
        self._contract_details = AsyncTtlLruCache[str, tuple[ContractInfo, ...]](
            max_entries=metadata_max_entries,
            ttl_seconds=metadata_ttl_seconds,
        )
        self._contract_search = AsyncTtlLruCache[str, tuple[ContractSearchResult, ...]](
            max_entries=metadata_max_entries,
            ttl_seconds=metadata_ttl_seconds,
        )
        self._option_chains = AsyncTtlLruCache[str, tuple[OptionChain, ...]](
            max_entries=metadata_max_entries,
            ttl_seconds=metadata_ttl_seconds,
        )
        self._quotes = AsyncTtlLruCache[str, MarketDataSnapshot](
            max_entries=quote_max_entries,
            ttl_seconds=quote_ttl_seconds,
        )
        self._market_data_routes = AsyncTtlLruCache[str, MarketDataType](
            max_entries=metadata_max_entries,
            ttl_seconds=metadata_ttl_seconds,
        )
        self._connection_token: tuple[bool, int | None, str | None] | None = None
        self._connection_lock = asyncio.Lock()

    async def start(self) -> None:
        await self._gateway.start()
        await self._clear()
        self._connection_token = self._status_token()

    async def stop(self) -> None:
        await self._clear()
        await self._gateway.stop()

    def status(self) -> GatewayStatus:
        return self._gateway.status()

    async def accounts(self) -> list[str]:
        return await self._gateway.accounts()

    async def account_summary(self, tags: frozenset[str]) -> list[AccountValue]:
        return await self._gateway.account_summary(tags)

    async def positions(self) -> list[Position]:
        return await self._gateway.positions()

    async def contract_details(self, contract: ContractSpec) -> list[ContractInfo]:
        await self._ensure_connection()
        key = contract.model_dump_json()
        values = await self._contract_details.get_or_load(
            key,
            lambda: self._load_contract_details(contract),
        )
        return list(values)

    async def search_contracts(self, pattern: str) -> list[ContractSearchResult]:
        await self._ensure_connection()
        key = pattern.strip().upper()
        values = await self._contract_search.get_or_load(
            key,
            lambda: self._load_contract_search(pattern),
        )
        return list(values)

    async def option_chains(self, request: OptionChainsRequest) -> list[OptionChain]:
        await self._ensure_connection()
        key = request.model_dump_json()
        values = await self._option_chains.get_or_load(
            key,
            lambda: self._load_option_chains(request),
        )
        return list(values)

    async def market_data_snapshot(
        self,
        request: MarketDataSnapshotRequest,
    ) -> MarketDataSnapshot:
        token = await self._ensure_connection()
        key = request.model_dump_json()
        found, snapshot = await self._quotes.lookup(key)
        if found:
            return cast(MarketDataSnapshot, snapshot)
        effective_request = request
        route_key = self._market_data_route_key(request)
        if route_key is not None:
            route_found, route = await self._market_data_routes.lookup(route_key)
            if route_found:
                effective_request = request.model_copy(
                    update={"market_data_type": cast(MarketDataType, route)}
                )
        try:
            snapshot = await self._gateway.market_data_snapshot(effective_request)
        except (GatewayRequestError, GatewayTimeout):
            if effective_request is request or route_key is None:
                raise
            await self._market_data_routes.delete(route_key)
            snapshot = await self._gateway.market_data_snapshot(request)
        if await self._ensure_connection() == token:
            await self._quotes.set(key, snapshot)
            if route_key is not None and self._should_cache_delayed_route(snapshot):
                await self._market_data_routes.set(route_key, snapshot.market_data_type)
        return snapshot

    async def historical_bars(self, request: HistoricalBarsRequest) -> list[HistoricalBar]:
        return await self._gateway.historical_bars(request)

    async def _load_contract_details(self, contract: ContractSpec) -> tuple[ContractInfo, ...]:
        return tuple(await self._gateway.contract_details(contract))

    async def _load_contract_search(self, pattern: str) -> tuple[ContractSearchResult, ...]:
        return tuple(await self._gateway.search_contracts(pattern))

    async def _load_option_chains(self, request: OptionChainsRequest) -> tuple[OptionChain, ...]:
        return tuple(await self._gateway.option_chains(request))

    async def _ensure_connection(self) -> tuple[bool, int | None, str | None]:
        token = self._status_token()
        async with self._connection_lock:
            if self._connection_token is None:
                self._connection_token = token
            elif token != self._connection_token:
                await self._clear()
                self._connection_token = token
        return token

    def _status_token(self) -> tuple[bool, int | None, str | None]:
        status = self._gateway.status()
        return status.connected, status.server_version, status.connection_time

    @staticmethod
    def _market_data_route_key(request: MarketDataSnapshotRequest) -> str | None:
        contract = request.contract
        if (
            request.market_data_type is not MarketDataType.AUTO
            or request.regulatory_snapshot
            or contract.symbol is None
            or contract.sec_type != "OPT"
        ):
            return None
        return "|".join(
            (
                contract.symbol,
                contract.sec_type,
                contract.exchange,
                contract.primary_exchange or "",
                contract.currency,
            )
        )

    def _should_cache_delayed_route(self, snapshot: MarketDataSnapshot) -> bool:
        return snapshot.market_data_type in {
            MarketDataType.DELAYED,
            MarketDataType.DELAYED_FROZEN,
        } and any(notice.code in self._DELAYED_ROUTE_NOTICE_CODES for notice in snapshot.notices)

    async def _clear(self) -> None:
        await asyncio.gather(
            self._contract_details.clear(),
            self._contract_search.clear(),
            self._option_chains.clear(),
            self._quotes.clear(),
            self._market_data_routes.clear(),
        )
