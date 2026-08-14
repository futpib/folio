from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class MarketDataType(StrEnum):
    AUTO = "auto"
    LIVE = "live"
    FROZEN = "frozen"
    DELAYED = "delayed"
    DELAYED_FROZEN = "delayed-frozen"


class ContractSpec(StrictModel):
    con_id: int | None = Field(default=None, ge=1)
    symbol: str | None = Field(default=None, min_length=1, max_length=32)
    sec_type: str = Field(default="STK", min_length=1, max_length=16)
    exchange: str = Field(default="SMART", min_length=1, max_length=32)
    primary_exchange: str | None = Field(default=None, min_length=1, max_length=32)
    currency: str = Field(default="USD", min_length=3, max_length=8)
    last_trade_date_or_contract_month: str | None = Field(default=None, max_length=32)
    strike: float | None = Field(default=None, ge=0)
    right: Literal["C", "P"] | None = None
    multiplier: str | None = Field(default=None, max_length=16)
    local_symbol: str | None = Field(default=None, max_length=64)
    trading_class: str | None = Field(default=None, max_length=64)

    @field_validator("symbol", "exchange", "primary_exchange", "currency", mode="before")
    @classmethod
    def uppercase_identifiers(cls, value: Any) -> Any:
        return value.upper() if isinstance(value, str) else value

    @field_validator("sec_type", mode="before")
    @classmethod
    def uppercase_sec_type(cls, value: Any) -> Any:
        return value.upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def require_identity(self) -> ContractSpec:
        if self.con_id is None and self.symbol is None and self.local_symbol is None:
            raise ValueError("one of con_id, symbol, or local_symbol is required")
        return self


class MarketDataSnapshotRequest(StrictModel):
    contract: ContractSpec
    market_data_type: MarketDataType = MarketDataType.AUTO
    generic_ticks: list[int] = Field(default_factory=list, max_length=32)
    regulatory_snapshot: bool = False


class OptionChainsRequest(StrictModel):
    underlying_symbol: str = Field(min_length=1, max_length=32)
    underlying_con_id: int = Field(ge=1)
    underlying_security_type: str = Field(default="STK", min_length=1, max_length=16)
    exchange: str = Field(default="", max_length=32)

    @field_validator(
        "underlying_symbol",
        "underlying_security_type",
        "exchange",
        mode="before",
    )
    @classmethod
    def uppercase_identifiers(cls, value: Any) -> Any:
        return value.upper() if isinstance(value, str) else value


class OptionChainSnapshotRequest(StrictModel):
    symbol: str = Field(min_length=1, max_length=32)
    expiration: str = Field(pattern=r"^\d{8}$")
    exchange: str = Field(default="SMART", min_length=1, max_length=32)
    currency: str = Field(default="USD", min_length=3, max_length=8)
    market_data_type: MarketDataType = MarketDataType.AUTO
    strike_min: float | None = Field(default=None, ge=0)
    strike_max: float | None = Field(default=None, ge=0)
    max_contracts: int = Field(default=40, ge=1, le=40)

    @field_validator("symbol", "exchange", "currency", mode="before")
    @classmethod
    def uppercase_identifiers(cls, value: Any) -> Any:
        return value.upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_strike_range(self) -> OptionChainSnapshotRequest:
        if (
            self.strike_min is not None
            and self.strike_max is not None
            and self.strike_min > self.strike_max
        ):
            raise ValueError("strike_min must not exceed strike_max")
        return self


class HistoricalBarsRequest(StrictModel):
    contract: ContractSpec
    end: datetime | None = None
    duration: str = Field(default="1 D", min_length=3, max_length=32)
    bar_size: str = Field(default="5 mins", min_length=3, max_length=32)
    data_type: str = Field(default="TRADES", min_length=1, max_length=32)
    regular_trading_hours_only: bool = True

    @field_validator("data_type", mode="before")
    @classmethod
    def uppercase_data_type(cls, value: Any) -> Any:
        return value.upper() if isinstance(value, str) else value

    @field_validator("end")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("end must include a UTC offset")
        return value


class StatusResponse(StrictModel):
    service: Literal["ok"] = "ok"
    tws_connected: bool
    server_version: int | None = None
    connection_time: str | None = None
    read_only: Literal[True] = True


class AccountResponse(StrictModel):
    account: str


class AccountValueResponse(StrictModel):
    account: str
    tag: str
    value: str
    currency: str


class ContractResponse(StrictModel):
    con_id: int
    symbol: str
    sec_type: str
    exchange: str
    primary_exchange: str | None = None
    currency: str
    local_symbol: str | None = None
    trading_class: str | None = None
    long_name: str | None = None
    min_tick: float | None = None
    last_trade_date_or_contract_month: str | None = None
    strike: float | None = None
    right: Literal["C", "P"] | None = None
    multiplier: str | None = None


class ContractSearchResponse(StrictModel):
    contract: ContractResponse
    derivative_security_types: list[str]


class OptionChainResponse(StrictModel):
    exchange: str
    underlying_con_id: int
    trading_class: str
    multiplier: str
    expirations: list[str]
    strikes: list[float]


class PositionResponse(StrictModel):
    account: str
    contract: ContractResponse
    position: Decimal
    average_cost: float


class OptionComputationResponse(StrictModel):
    option_price: float | None = None
    implied_volatility: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    underlying_price: float | None = None
    present_value_dividend: float | None = None


class OptionComputationsResponse(StrictModel):
    bid: OptionComputationResponse | None = None
    ask: OptionComputationResponse | None = None
    last: OptionComputationResponse | None = None
    model: OptionComputationResponse | None = None


class TwsErrorDetailResponse(StrictModel):
    code: int | None = None
    message: str


class MarketDataSnapshotResponse(StrictModel):
    contract: ContractSpec
    requested_market_data_type: MarketDataType
    market_data_type: MarketDataType
    quote_complete: bool
    enrichment_complete: bool
    complete: bool
    collected_at: datetime
    last_trade_time: datetime | None = None
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    mark: float | None = None
    bid_size: float | int | None = None
    ask_size: float | int | None = None
    last_size: float | int | None = None
    volume: float | int | None = None
    open_interest: float | int | None = None
    option_computations: OptionComputationsResponse | None = None
    tws_notices: list[TwsErrorDetailResponse] = Field(default_factory=list)
    fields: dict[str, float | int | str | None]


class TwsErrorResponse(StrictModel):
    detail: Literal["TWS rejected the request"] = "TWS rejected the request"
    tws_error: TwsErrorDetailResponse


class GatewayTimeoutResponse(StrictModel):
    detail: Literal["TWS request timed out"] = "TWS request timed out"


class OptionChainQuoteResponse(StrictModel):
    contract: ContractResponse
    snapshot: MarketDataSnapshotResponse | None = None
    error: TwsErrorDetailResponse | None = None

    @model_validator(mode="after")
    def require_snapshot_or_error(self) -> OptionChainQuoteResponse:
        if (self.snapshot is None) == (self.error is None):
            raise ValueError("exactly one of snapshot or error is required")
        return self


class OptionChainSnapshotResponse(StrictModel):
    underlying: ContractResponse
    underlying_snapshot: MarketDataSnapshotResponse | None = None
    underlying_error: TwsErrorDetailResponse | None = None
    expiration: str
    exchange: str
    requested_market_data_type: MarketDataType
    quote_complete: bool
    enrichment_complete: bool
    complete: bool
    collection_started_at: datetime
    collected_at: datetime
    quotes: list[OptionChainQuoteResponse]

    @model_validator(mode="after")
    def require_underlying_snapshot_or_error(self) -> OptionChainSnapshotResponse:
        if (self.underlying_snapshot is None) == (self.underlying_error is None):
            raise ValueError("exactly one of underlying_snapshot or underlying_error is required")
        return self


class HistoricalBarResponse(StrictModel):
    time: str
    open: float
    high: float
    low: float
    close: float
    volume: Decimal
    weighted_average_price: Decimal
    trade_count: int
