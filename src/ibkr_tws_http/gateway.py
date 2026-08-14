from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from .models import (
    ContractSpec,
    HistoricalBarsRequest,
    MarketDataSnapshotRequest,
    OptionChainsRequest,
)


class GatewayError(RuntimeError):
    """Base error for TWS gateway operations."""


class GatewayUnavailable(GatewayError):
    """The local TWS API is not currently connected."""


class GatewayTimeout(GatewayError):
    """TWS did not complete a request before its deadline."""


class GatewayRequestError(GatewayError):
    """TWS rejected or failed a request."""


@dataclass(frozen=True, slots=True)
class GatewayStatus:
    connected: bool
    server_version: int | None = None
    connection_time: str | None = None


@dataclass(frozen=True, slots=True)
class AccountValue:
    account: str
    tag: str
    value: str
    currency: str


@dataclass(frozen=True, slots=True)
class ContractInfo:
    con_id: int
    symbol: str
    sec_type: str
    exchange: str
    primary_exchange: str | None
    currency: str
    local_symbol: str | None
    trading_class: str | None
    long_name: str | None = None
    min_tick: float | None = None


@dataclass(frozen=True, slots=True)
class Position:
    account: str
    contract: ContractInfo
    position: Decimal
    average_cost: float


@dataclass(frozen=True, slots=True)
class ContractSearchResult:
    contract: ContractInfo
    derivative_security_types: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OptionChain:
    exchange: str
    underlying_con_id: int
    trading_class: str
    multiplier: str
    expirations: tuple[str, ...]
    strikes: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class HistoricalBar:
    time: str
    open: float
    high: float
    low: float
    close: float
    volume: Decimal
    weighted_average_price: Decimal
    trade_count: int


class Gateway(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    def status(self) -> GatewayStatus: ...

    async def accounts(self) -> list[str]: ...

    async def account_summary(self, tags: frozenset[str]) -> list[AccountValue]: ...

    async def positions(self) -> list[Position]: ...

    async def contract_details(self, contract: ContractSpec) -> list[ContractInfo]: ...

    async def search_contracts(self, pattern: str) -> list[ContractSearchResult]: ...

    async def option_chains(self, request: OptionChainsRequest) -> list[OptionChain]: ...

    async def market_data_snapshot(
        self, request: MarketDataSnapshotRequest
    ) -> dict[str, float | int | str | None]: ...

    async def historical_bars(self, request: HistoricalBarsRequest) -> list[HistoricalBar]: ...
