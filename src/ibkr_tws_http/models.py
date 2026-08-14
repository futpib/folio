from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


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


class MarketDataSnapshotResponse(StrictModel):
    contract: ContractSpec
    fields: dict[str, float | int | str | None]


class HistoricalBarResponse(StrictModel):
    time: str
    open: float
    high: float
    low: float
    close: float
    volume: Decimal
    weighted_average_price: Decimal
    trade_count: int
