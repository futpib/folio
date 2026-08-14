from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from .models import (
    ContractSpec,
    HistoricalBarsRequest,
    MarketDataSnapshotRequest,
    MarketDataType,
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

    def __init__(
        self,
        message: str,
        code: int | None = None,
        sensitive_values: tuple[str, ...] = (),
    ):
        super().__init__(message)
        self.code = code
        self.public_message = redact_tws_error(message, sensitive_values)


_ACCOUNT_ID = re.compile(r"(?<![A-Z0-9])(?:DU|DF|FA|F|U)\d{3,}(?![A-Z0-9])", re.IGNORECASE)


def redact_tws_error(message: str, sensitive_values: tuple[str, ...] = ()) -> str:
    redacted = message
    for value in sensitive_values:
        if value:
            redacted = redacted.replace(value, "[redacted account]")
    return _ACCOUNT_ID.sub("[redacted account]", redacted)[:500]


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
    last_trade_date_or_contract_month: str | None = None
    strike: float | None = None
    right: str | None = None
    multiplier: str | None = None


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


@dataclass(frozen=True, slots=True)
class OptionComputation:
    option_price: float | None
    implied_volatility: float | None
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None
    underlying_price: float | None
    present_value_dividend: float | None


@dataclass(frozen=True, slots=True)
class TwsNotice:
    code: int | None
    message: str


@dataclass(frozen=True, slots=True)
class MarketDataSnapshot:
    market_data_type: MarketDataType
    as_of: datetime
    complete: bool
    fields: dict[str, float | int | str | None]
    option_computations: dict[str, OptionComputation]
    notices: tuple[TwsNotice, ...] = ()


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
    ) -> MarketDataSnapshot: ...

    async def historical_bars(self, request: HistoricalBarsRequest) -> list[HistoricalBar]: ...
