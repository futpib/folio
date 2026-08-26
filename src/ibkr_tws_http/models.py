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
    max_concurrency: int = Field(
        default=10,
        ge=1,
        le=40,
        description="Maximum simultaneous underlying and option quote collections",
    )
    timeout_seconds: float | None = Field(
        default=None,
        ge=1,
        le=120,
        description="Global batch collection window; defaults to at most 10 seconds",
    )

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


class VolatilityHistoryRequest(StrictModel):
    contract: ContractSpec
    end: datetime | None = None
    duration: str = Field(default="1 M", min_length=3, max_length=32)
    bar_size: str = Field(default="1 day", min_length=3, max_length=32)
    regular_trading_hours_only: bool = True

    @field_validator("end")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("end must include a UTC offset")
        return value

    @model_validator(mode="after")
    def require_supported_underlying(self) -> VolatilityHistoryRequest:
        if self.contract.sec_type not in {"STK", "IND"}:
            raise ValueError("volatility history supports only stock, ETF, and index underlyings")
        return self


class PortfolioOptionSnapshotRequest(StrictModel):
    market_data_type: MarketDataType = MarketDataType.AUTO
    max_concurrency: int = Field(default=10, ge=1, le=40)
    timeout_seconds: float | None = Field(default=None, ge=1, le=120)


class OptionVolatilityComparisonRequest(StrictModel):
    option_contract: ContractSpec
    underlying_contract: ContractSpec
    market_data_type: MarketDataType = MarketDataType.AUTO
    end: datetime | None = None
    duration: str = Field(default="1 M", min_length=3, max_length=32)
    bar_size: str = Field(default="1 day", min_length=3, max_length=32)
    regular_trading_hours_only: bool = True

    @field_validator("end")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("end must include a UTC offset")
        return value

    @model_validator(mode="after")
    def require_option_and_underlying(self) -> OptionVolatilityComparisonRequest:
        if self.option_contract.sec_type != "OPT":
            raise ValueError("option_contract must have sec_type OPT")
        if self.underlying_contract.sec_type not in {"STK", "IND"}:
            raise ValueError("underlying_contract must have sec_type STK or IND")
        return self


class ObservedOptionHistoryRequest(StrictModel):
    contract: ContractSpec
    start: datetime | None = None
    end: datetime | None = None
    limit: int = Field(default=1000, ge=1, le=10000)

    @field_validator("start", "end")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("history bounds must include a UTC offset")
        return value

    @model_validator(mode="after")
    def require_option(self) -> ObservedOptionHistoryRequest:
        if self.contract.sec_type != "OPT":
            raise ValueError("observed option history supports only OPT contracts")
        if self.start is not None and self.end is not None and self.start > self.end:
            raise ValueError("start must not exceed end")
        return self


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


class ImpliedVolatilityResponse(StrictModel):
    value: float | None = Field(
        description="Quality-gated IV as a decimal, or null when neither source is usable",
    )
    source: Literal["market_mid", "model"] | None = Field(
        description="market_mid for a qualifying two-sided quote; model for IB model fallback",
    )
    bid_implied_volatility: float | None = Field(
        description="IB IV calculated from the actual option bid, as a decimal",
    )
    ask_implied_volatility: float | None = Field(
        description="IB IV calculated from the actual option ask, as a decimal",
    )
    quote_observed_at: datetime | None = Field(
        description="Oldest server observation time among the bid, ask, and their IVs",
    )
    quote_age_seconds: float | None = Field(
        description="Age of quote_observed_at when collection completed",
    )
    spread_percentage: float | None = Field(
        description="Option ask minus bid divided by their midpoint, in percent",
    )
    market_mid_eligible: bool
    market_mid_rejection_reason: (
        Literal[
            "missing_bid_or_ask",
            "crossed_quote",
            "missing_bid_or_ask_iv",
            "inverted_iv_bounds",
            "missing_quote_timestamp",
            "stale_quote",
        ]
        | None
    ) = None
    max_quote_age_seconds: float = Field(
        description="Maximum server-observed quote age accepted for market_mid",
    )


class TwsErrorDetailResponse(StrictModel):
    code: int | None = None
    message: str


class MarketDataSnapshotResponse(StrictModel):
    contract: ContractSpec
    requested_market_data_type: MarketDataType
    market_data_type: MarketDataType
    market_quote_complete: bool = Field(
        description="An actual bid, ask, or last price was received",
    )
    quote_complete: bool = Field(
        description="Compatibility alias for market_quote_complete",
    )
    model_complete: bool | None = Field(
        default=None,
        description="Option model price, IV, and Greeks are complete; null when not applicable",
    )
    enrichment_complete: bool = Field(
        description="Requested option volume, open interest, and mark fields are complete",
    )
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
    implied_volatility: ImpliedVolatilityResponse | None = Field(
        default=None,
        description="Quality-gated market-mid IV with an explicitly labelled model fallback",
    )
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
    attempts: int = Field(ge=1, le=2)

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
    market_quote_complete: bool
    quote_complete: bool
    model_complete: bool
    enrichment_complete: bool
    complete: bool
    collection_timeout_seconds: float
    max_concurrency: int
    preflight_attempted: bool
    preflight_market_data_type: MarketDataType | None = None
    preflight_seconds: float = Field(ge=0)
    retried_contracts: int = Field(ge=0)
    collection_started_at: datetime
    collected_at: datetime
    underlying_attempts: int = Field(ge=1, le=2)
    reference_underlying_price: float | None = None
    reference_underlying_price_source: Literal["option_model_median"] | None = None
    reference_underlying_price_collected_at: datetime | None = None
    reference_underlying_price_samples: int = Field(default=0, ge=0)
    quotes: list[OptionChainQuoteResponse]

    @model_validator(mode="after")
    def require_underlying_snapshot_or_error(self) -> OptionChainSnapshotResponse:
        if (self.underlying_snapshot is None) == (self.underlying_error is None):
            raise ValueError("exactly one of underlying_snapshot or underlying_error is required")
        return self


class PortfolioOptionQuoteResponse(StrictModel):
    account: str
    position: Decimal
    average_cost: float
    contract: ContractResponse
    snapshot: MarketDataSnapshotResponse | None = None
    error: TwsErrorDetailResponse | None = None
    attempts: int = Field(ge=1, le=2)

    @model_validator(mode="after")
    def require_snapshot_or_error(self) -> PortfolioOptionQuoteResponse:
        if (self.snapshot is None) == (self.error is None):
            raise ValueError("exactly one of snapshot or error is required")
        return self


class PortfolioOptionSnapshotResponse(StrictModel):
    requested_market_data_type: MarketDataType
    position_count: int = Field(ge=0)
    returned_count: int = Field(ge=0)
    coverage_percentage: float = Field(ge=0, le=100)
    collection_timeout_seconds: float
    max_concurrency: int
    retried_contracts: int = Field(ge=0)
    collection_started_at: datetime
    collected_at: datetime
    quotes: list[PortfolioOptionQuoteResponse]


class HistoricalBarResponse(StrictModel):
    time: str
    open: float
    high: float
    low: float
    close: float
    volume: Decimal
    weighted_average_price: Decimal
    trade_count: int


class VolatilitySeriesResponse(StrictModel):
    data_type: Literal["OPTION_IMPLIED_VOLATILITY", "HISTORICAL_VOLATILITY"]
    name: Literal[
        "ib_30_day_at_market_implied_volatility",
        "ib_30_day_historical_volatility",
    ]
    window_calendar_days: Literal[30] = 30
    units: Literal["decimal"] = "decimal"
    bars: list[HistoricalBarResponse]


class VolatilityHistoryResponse(StrictModel):
    contract: ContractSpec
    end: datetime | None = None
    duration: str
    bar_size: str
    regular_trading_hours_only: bool
    implied_volatility: VolatilitySeriesResponse
    historical_volatility: VolatilitySeriesResponse


class VolatilityComparisonMetricsResponse(StrictModel):
    units: Literal["decimal"] = "decimal"
    current_option_implied_volatility: float | None = None
    current_option_implied_volatility_source: Literal["market_mid", "model"] | None = None
    latest_underlying_implied_volatility: float | None = None
    latest_underlying_historical_volatility: float | None = None
    option_premium_to_underlying_implied: float | None = None
    option_ratio_to_underlying_implied: float | None = None
    option_premium_to_underlying_historical: float | None = None
    option_ratio_to_underlying_historical: float | None = None
    option_percentile_in_underlying_implied_history: float | None = Field(
        default=None,
        ge=0,
        le=100,
    )
    option_z_score_in_underlying_implied_history: float | None = None
    comparison_note: Literal[
        "The current exact-option IV is compared with IB's 30-day underlying volatility series; "
        "this is not historical IV for the exact option."
    ] = (
        "The current exact-option IV is compared with IB's 30-day underlying volatility series; "
        "this is not historical IV for the exact option."
    )


class OptionVolatilityComparisonResponse(StrictModel):
    option_snapshot: MarketDataSnapshotResponse
    underlying_history: VolatilityHistoryResponse
    metrics: VolatilityComparisonMetricsResponse


class ObservedOptionSnapshotResponse(StrictModel):
    collected_at: datetime
    implied_volatility: float
    source: Literal["market_mid", "model"]
    bid: float | None = None
    ask: float | None = None
    quote_observed_at: datetime | None = None
    quote_age_seconds: float | None = None
    spread_percentage: float | None = None


class ObservedOptionHistoryResponse(StrictModel):
    contract: ContractSpec
    continuous_coverage: Literal[False] = False
    coverage_note: Literal[
        "Request-driven observations only; gaps mean the API was not asked for this contract."
    ] = "Request-driven observations only; gaps mean the API was not asked for this contract."
    first_observation_at: datetime | None = None
    last_observation_at: datetime | None = None
    samples: list[ObservedOptionSnapshotResponse]


class CapabilityStatusResponse(StrictModel):
    status: Literal["unknown", "available", "unavailable"]
    evidence_codes: list[int] = Field(default_factory=list)


class CapabilitiesResponse(StrictModel):
    tws_connected: bool
    live_market_quotes: CapabilityStatusResponse
    delayed_option_model: CapabilityStatusResponse
    exact_option_historical_data: CapabilityStatusResponse
    opportunistic_option_history_enabled: bool
    opportunistic_option_history_samples: int = Field(ge=0)
    adaptive_batch_scheduler: Literal[True] = True
    continuous_uptime_required: Literal[False] = False
    additional_market_data_subscriptions_required: Literal[False] = False
