from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from statistics import mean, median, pstdev
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from . import __version__
from .batch import BatchItemResult, collect_market_data_batch
from .cache import CachingGateway
from .config import Settings
from .diagnostics import CapabilityTracker
from .gateway import (
    ContractInfo,
    Gateway,
    GatewayError,
    GatewayRequestError,
    GatewayTimeout,
    GatewayUnavailable,
    HistoricalBar,
    MarketDataSnapshot,
    OptionComputation,
)
from .history import OptionSnapshotJournal
from .models import (
    AccountResponse,
    AccountValueResponse,
    CapabilitiesResponse,
    ContractResponse,
    ContractSearchResponse,
    ContractSpec,
    GatewayTimeoutResponse,
    HistoricalBarResponse,
    HistoricalBarsRequest,
    ImpliedVolatilityResponse,
    MarketDataSnapshotRequest,
    MarketDataSnapshotResponse,
    MarketDataType,
    ObservedOptionHistoryRequest,
    ObservedOptionHistoryResponse,
    OptionChainQuoteResponse,
    OptionChainResponse,
    OptionChainSnapshotRequest,
    OptionChainSnapshotResponse,
    OptionChainsRequest,
    OptionComputationResponse,
    OptionComputationsResponse,
    OptionVolatilityComparisonRequest,
    OptionVolatilityComparisonResponse,
    PortfolioOptionQuoteResponse,
    PortfolioOptionSnapshotRequest,
    PortfolioOptionSnapshotResponse,
    PositionResponse,
    StatusResponse,
    TwsErrorDetailResponse,
    TwsErrorResponse,
    VolatilityComparisonMetricsResponse,
    VolatilityHistoryRequest,
    VolatilityHistoryResponse,
    VolatilitySeriesResponse,
)
from .official import OfficialGateway
from .policy import ExposurePolicy, PolicyDenied


def _contract_response(contract: ContractInfo) -> ContractResponse:
    return ContractResponse(
        con_id=contract.con_id,
        symbol=contract.symbol,
        sec_type=contract.sec_type,
        exchange=contract.exchange,
        primary_exchange=contract.primary_exchange,
        currency=contract.currency,
        local_symbol=contract.local_symbol,
        trading_class=contract.trading_class,
        long_name=contract.long_name,
        min_tick=contract.min_tick,
        last_trade_date_or_contract_month=contract.last_trade_date_or_contract_month,
        strike=contract.strike,
        right=contract.right,
        multiplier=contract.multiplier,
    )


def _contract_spec(contract: ContractInfo) -> ContractSpec:
    return ContractSpec(
        con_id=contract.con_id,
        symbol=contract.symbol,
        sec_type=contract.sec_type,
        exchange=contract.exchange or "SMART",
        primary_exchange=contract.primary_exchange,
        currency=contract.currency or "USD",
        last_trade_date_or_contract_month=contract.last_trade_date_or_contract_month,
        strike=contract.strike,
        right=contract.right,
        multiplier=contract.multiplier,
        local_symbol=contract.local_symbol,
        trading_class=contract.trading_class,
    )


def _available_number(
    fields: dict[str, float | int | str | None],
    names: Iterable[str],
    *,
    nonnegative: bool = False,
) -> float | int | None:
    for name in names:
        value = fields.get(name)
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and abs(value) < 1e100
            and value != -1
            and (not nonnegative or value >= 0)
        ):
            return value
    return None


def _last_trade_time(fields: dict[str, float | int | str | None]) -> datetime | None:
    value = fields.get("last_timestamp") or fields.get("delayed_last_timestamp")
    milliseconds = False
    if value is None:
        value = fields.get("last_reg_time")
        milliseconds = value is not None
    if value is None:
        rt_volume = fields.get("rt_volume")
        if isinstance(rt_volume, str):
            parts = rt_volume.split(";")
            if len(parts) >= 3:
                value = parts[2]
                milliseconds = True
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return None
    if milliseconds or timestamp > 10_000_000_000:
        timestamp /= 1000
    try:
        return datetime.fromtimestamp(timestamp, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _option_computation(value: OptionComputation | None) -> OptionComputationResponse | None:
    if value is None:
        return None
    return OptionComputationResponse(
        option_price=value.option_price,
        implied_volatility=value.implied_volatility,
        delta=value.delta,
        gamma=value.gamma,
        theta=value.theta,
        vega=value.vega,
        underlying_price=value.underlying_price,
        present_value_dividend=value.present_value_dividend,
    )


def _option_side_price(
    fields: dict[str, float | int | str | None],
    names: Iterable[str],
    computation: OptionComputation | None,
) -> float | int | None:
    value = _available_number(fields, names)
    if value is not None or computation is None:
        return value
    option_price = computation.option_price
    if (
        option_price is None
        or not math.isfinite(option_price)
        or abs(option_price) > 1e100
        or option_price == -1
    ):
        return None
    return option_price


_MAX_MARKET_MID_QUOTE_AGE_SECONDS = 5.0


def _option_side_observed_at(
    snapshot: MarketDataSnapshot,
    names: Iterable[str],
    computation_name: str,
) -> datetime | None:
    for name in names:
        if _available_number(snapshot.fields, (name,)) is not None:
            return snapshot.field_observed_at.get(name)
    computation = snapshot.option_computations.get(computation_name)
    if computation is not None and _option_side_price({}, (), computation) is not None:
        return snapshot.option_computation_observed_at.get(computation_name)
    return None


def _valid_implied_volatility(value: float | None) -> float | None:
    if value is None or not math.isfinite(value) or value <= 0 or value >= 1e100:
        return None
    return value


def _implied_volatility_response(
    snapshot: MarketDataSnapshot,
    bid: float | int | None,
    ask: float | int | None,
) -> ImpliedVolatilityResponse:
    bid_computation = snapshot.option_computations.get("bid")
    ask_computation = snapshot.option_computations.get("ask")
    model_computation = snapshot.option_computations.get("model")
    bid_iv = _valid_implied_volatility(
        bid_computation.implied_volatility if bid_computation is not None else None
    )
    ask_iv = _valid_implied_volatility(
        ask_computation.implied_volatility if ask_computation is not None else None
    )
    model_iv = _valid_implied_volatility(
        model_computation.implied_volatility if model_computation is not None else None
    )

    spread_percentage = None
    if bid is not None and ask is not None and bid >= 0 and ask >= 0 and bid + ask > 0:
        spread_percentage = (ask - bid) / ((bid + ask) / 2) * 100

    observed_values = (
        _option_side_observed_at(snapshot, ("bid", "delayed_bid"), "bid"),
        _option_side_observed_at(snapshot, ("ask", "delayed_ask"), "ask"),
        snapshot.option_computation_observed_at.get("bid"),
        snapshot.option_computation_observed_at.get("ask"),
    )
    quote_observed_at = min(observed_values) if all(observed_values) else None
    quote_age_seconds = (
        max(0.0, (snapshot.as_of - quote_observed_at).total_seconds())
        if quote_observed_at is not None
        else None
    )

    reason = None
    if bid is None or ask is None or bid < 0 or ask < 0:
        reason = "missing_bid_or_ask"
    elif ask < bid:
        reason = "crossed_quote"
    elif bid_iv is None or ask_iv is None:
        reason = "missing_bid_or_ask_iv"
    elif ask_iv < bid_iv:
        reason = "inverted_iv_bounds"
    elif quote_observed_at is None:
        reason = "missing_quote_timestamp"
    elif quote_age_seconds is None or quote_age_seconds > _MAX_MARKET_MID_QUOTE_AGE_SECONDS:
        reason = "stale_quote"

    market_mid_eligible = reason is None
    if market_mid_eligible:
        value = (bid_iv + ask_iv) / 2
        source = "market_mid"
    else:
        value = model_iv
        source = "model" if model_iv is not None else None

    return ImpliedVolatilityResponse(
        value=value,
        source=source,
        bid_implied_volatility=bid_iv,
        ask_implied_volatility=ask_iv,
        quote_observed_at=quote_observed_at,
        quote_age_seconds=quote_age_seconds,
        spread_percentage=spread_percentage,
        market_mid_eligible=market_mid_eligible,
        market_mid_rejection_reason=reason,
        max_quote_age_seconds=_MAX_MARKET_MID_QUOTE_AGE_SECONDS,
    )


def _market_data_response(
    contract: ContractSpec,
    requested_type: MarketDataType,
    snapshot: MarketDataSnapshot,
) -> MarketDataSnapshotResponse:
    fields = snapshot.fields
    right = contract.right
    volume_names = ["volume", "delayed_volume"]
    open_interest_names = ["open_interest", "futures_open_interest"]
    if right == "C":
        volume_names.insert(0, "option_call_volume")
        open_interest_names.insert(0, "option_call_open_interest")
    elif right == "P":
        volume_names.insert(0, "option_put_volume")
        open_interest_names.insert(0, "option_put_open_interest")
    computations = OptionComputationsResponse(
        bid=_option_computation(snapshot.option_computations.get("bid")),
        ask=_option_computation(snapshot.option_computations.get("ask")),
        last=_option_computation(snapshot.option_computations.get("last")),
        model=_option_computation(snapshot.option_computations.get("model")),
    )
    if not any((computations.bid, computations.ask, computations.last, computations.model)):
        computations = None
    bid = _option_side_price(
        fields,
        ("bid", "delayed_bid"),
        snapshot.option_computations.get("bid"),
    )
    ask = _option_side_price(
        fields,
        ("ask", "delayed_ask"),
        snapshot.option_computations.get("ask"),
    )
    return MarketDataSnapshotResponse(
        contract=contract,
        requested_market_data_type=requested_type,
        market_data_type=snapshot.market_data_type,
        market_quote_complete=snapshot.market_quote_complete,
        quote_complete=snapshot.market_quote_complete,
        model_complete=(snapshot.model_complete if contract.sec_type == "OPT" else None),
        enrichment_complete=snapshot.enrichment_complete,
        complete=snapshot.complete,
        collected_at=snapshot.as_of,
        last_trade_time=_last_trade_time(fields),
        bid=bid,
        ask=ask,
        last=_option_side_price(
            fields,
            ("last", "delayed_last"),
            snapshot.option_computations.get("last"),
        ),
        mark=_available_number(
            fields,
            ("mark_price", "creditman_mark_price", "creditman_slow_mark_price"),
        ),
        bid_size=_available_number(fields, ("bid_size", "delayed_bid_size"), nonnegative=True),
        ask_size=_available_number(fields, ("ask_size", "delayed_ask_size"), nonnegative=True),
        last_size=_available_number(fields, ("last_size", "delayed_last_size"), nonnegative=True),
        volume=_available_number(fields, volume_names, nonnegative=True),
        open_interest=_available_number(fields, open_interest_names, nonnegative=True),
        option_computations=computations,
        implied_volatility=(
            _implied_volatility_response(snapshot, bid, ask) if contract.sec_type == "OPT" else None
        ),
        tws_notices=[
            TwsErrorDetailResponse(code=notice.code, message=notice.message)
            for notice in snapshot.notices
        ],
        fields=fields,
    )


def _tws_error(error: GatewayRequestError) -> TwsErrorDetailResponse:
    return TwsErrorDetailResponse(code=error.code, message=error.public_message)


def _market_data_error(
    error: GatewayRequestError | GatewayTimeout,
) -> TwsErrorDetailResponse:
    if isinstance(error, GatewayRequestError):
        return _tws_error(error)
    return TwsErrorDetailResponse(code=None, message="TWS market data request timed out")


def _historical_bar_response(value: HistoricalBar) -> HistoricalBarResponse:
    return HistoricalBarResponse(
        time=value.time,
        open=value.open,
        high=value.high,
        low=value.low,
        close=value.close,
        volume=value.volume,
        weighted_average_price=value.weighted_average_price,
        trade_count=value.trade_count,
    )


def _volatility_history_response(
    request: VolatilityHistoryRequest,
    implied_values: list[HistoricalBar],
    historical_values: list[HistoricalBar],
) -> VolatilityHistoryResponse:
    return VolatilityHistoryResponse(
        contract=request.contract,
        end=request.end,
        duration=request.duration,
        bar_size=request.bar_size,
        regular_trading_hours_only=request.regular_trading_hours_only,
        implied_volatility=VolatilitySeriesResponse(
            data_type="OPTION_IMPLIED_VOLATILITY",
            name="ib_30_day_at_market_implied_volatility",
            bars=[_historical_bar_response(value) for value in implied_values],
        ),
        historical_volatility=VolatilitySeriesResponse(
            data_type="HISTORICAL_VOLATILITY",
            name="ib_30_day_historical_volatility",
            bars=[_historical_bar_response(value) for value in historical_values],
        ),
    )


def _positive_closes(values: list[HistoricalBar]) -> list[float]:
    return [value.close for value in values if math.isfinite(value.close) and value.close > 0]


def _volatility_comparison_metrics(
    snapshot: MarketDataSnapshotResponse,
    implied_values: list[HistoricalBar],
    historical_values: list[HistoricalBar],
) -> VolatilityComparisonMetricsResponse:
    option_iv = snapshot.implied_volatility
    current = option_iv.value if option_iv is not None else None
    implied = _positive_closes(implied_values)
    historical = _positive_closes(historical_values)
    latest_implied = implied[-1] if implied else None
    latest_historical = historical[-1] if historical else None

    def premium(reference: float | None) -> float | None:
        return current - reference if current is not None and reference is not None else None

    def ratio(reference: float | None) -> float | None:
        return current / reference if current is not None and reference is not None else None

    percentile = None
    z_score = None
    if current is not None and implied:
        percentile = sum(value <= current for value in implied) / len(implied) * 100
        deviation = pstdev(implied)
        if deviation > 0:
            z_score = (current - mean(implied)) / deviation
    return VolatilityComparisonMetricsResponse(
        current_option_implied_volatility=current,
        current_option_implied_volatility_source=(option_iv.source if option_iv else None),
        latest_underlying_implied_volatility=latest_implied,
        latest_underlying_historical_volatility=latest_historical,
        option_premium_to_underlying_implied=premium(latest_implied),
        option_ratio_to_underlying_implied=ratio(latest_implied),
        option_premium_to_underlying_historical=premium(latest_historical),
        option_ratio_to_underlying_historical=ratio(latest_historical),
        option_percentile_in_underlying_implied_history=percentile,
        option_z_score_in_underlying_implied_history=z_score,
    )


TWS_ERROR_RESPONSES = {
    502: {"model": TwsErrorResponse},
    504: {"model": GatewayTimeoutResponse},
}


def create_app(settings: Settings, gateway: Gateway | None = None) -> FastAPI:
    settings.validate()
    active_gateway = CachingGateway(gateway or OfficialGateway(settings.tws))
    policy = ExposurePolicy(settings.exposure)
    journal = OptionSnapshotJournal(settings.history.path)
    capabilities = CapabilityTracker()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await journal.start()
        try:
            await active_gateway.start()
            try:
                yield
            finally:
                await active_gateway.stop()
        finally:
            await journal.stop()

    app = FastAPI(
        title="IBKR TWS HTTP",
        version=__version__,
        description="A deliberately narrow, read-only facade for a local TWS API.",
        lifespan=lifespan,
    )
    app.state.gateway = active_gateway
    app.state.settings = settings
    app.state.journal = journal
    app.state.capabilities = capabilities

    async def observe_snapshot(snapshot: MarketDataSnapshotResponse) -> None:
        capabilities.observe_snapshot(snapshot)
        await journal.record(snapshot)

    def observe_batch_error(result: BatchItemResult) -> None:
        if isinstance(result.error, GatewayRequestError):
            capabilities.observe_error(result.error)

    async def load_volatility_history(
        request: VolatilityHistoryRequest,
    ) -> tuple[VolatilityHistoryResponse, list[HistoricalBar], list[HistoricalBar]]:
        common = {
            "contract": request.contract,
            "end": request.end,
            "duration": request.duration,
            "bar_size": request.bar_size,
            "regular_trading_hours_only": request.regular_trading_hours_only,
        }
        implied_values, historical_values = await asyncio.gather(
            active_gateway.historical_bars(
                HistoricalBarsRequest(**common, data_type="OPTION_IMPLIED_VOLATILITY")
            ),
            active_gateway.historical_bars(
                HistoricalBarsRequest(**common, data_type="HISTORICAL_VOLATILITY")
            ),
        )
        return (
            _volatility_history_response(request, implied_values, historical_values),
            implied_values,
            historical_values,
        )

    @app.exception_handler(PolicyDenied)
    async def policy_error(request: Request, exc: PolicyDenied) -> JSONResponse:
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @app.exception_handler(GatewayUnavailable)
    async def unavailable_error(request: Request, exc: GatewayUnavailable) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": "TWS is not connected"})

    @app.exception_handler(GatewayTimeout)
    async def timeout_error(request: Request, exc: GatewayTimeout) -> JSONResponse:
        return JSONResponse(status_code=504, content={"detail": "TWS request timed out"})

    @app.exception_handler(GatewayRequestError)
    async def request_error(request: Request, exc: GatewayRequestError) -> JSONResponse:
        capabilities.observe_error(exc)
        return JSONResponse(
            status_code=502,
            content=TwsErrorResponse(tws_error=_tws_error(exc)).model_dump(mode="json"),
        )

    @app.exception_handler(GatewayError)
    async def gateway_error(request: Request, exc: GatewayError) -> JSONResponse:
        return JSONResponse(status_code=502, content={"detail": "TWS request failed"})

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"service": "ok"}

    @app.get("/v1/status", response_model=StatusResponse)
    async def status() -> StatusResponse:
        policy.require_feature("status")
        current = active_gateway.status()
        return StatusResponse(
            tws_connected=current.connected,
            server_version=current.server_version,
            connection_time=current.connection_time,
        )

    @app.get("/v1/capabilities", response_model=CapabilitiesResponse)
    async def capability_status() -> CapabilitiesResponse:
        current = active_gateway.status()
        return capabilities.response(
            tws_connected=current.connected,
            journal_enabled=journal.enabled,
            journal_samples=await journal.count(),
        )

    @app.get("/v1/accounts", response_model=list[AccountResponse])
    async def accounts() -> list[AccountResponse]:
        policy.require_feature("accounts")
        values = policy.filter_accounts(await active_gateway.accounts())
        return [AccountResponse(account=account) for account in values]

    @app.get("/v1/account-summary", response_model=list[AccountValueResponse])
    async def account_summary() -> list[AccountValueResponse]:
        policy.require_feature("account-summary")
        values = await active_gateway.account_summary(settings.exposure.account_summary_tags)
        return [
            AccountValueResponse(
                account=value.account,
                tag=value.tag,
                value=value.value,
                currency=value.currency,
            )
            for value in policy.filter_account_summary(values)
        ]

    @app.get("/v1/positions", response_model=list[PositionResponse])
    async def positions() -> list[PositionResponse]:
        policy.require_feature("positions")
        values = policy.filter_positions(await active_gateway.positions())
        return [
            PositionResponse(
                account=value.account,
                contract=_contract_response(value.contract),
                position=value.position,
                average_cost=value.average_cost,
            )
            for value in values
        ]

    @app.post("/v1/contracts/details", response_model=list[ContractResponse])
    async def contract_details(contract: ContractSpec) -> list[ContractResponse]:
        policy.require_feature("contract-details")
        policy.check_contract(contract)
        values = policy.filter_contracts(await active_gateway.contract_details(contract))
        return [_contract_response(value) for value in values]

    @app.get("/v1/contracts/search", response_model=list[ContractSearchResponse])
    async def contract_search(
        pattern: Annotated[str, Query(min_length=1, max_length=64)],
    ) -> list[ContractSearchResponse]:
        policy.require_feature("contract-search")
        values = policy.filter_contract_search(await active_gateway.search_contracts(pattern))
        return [
            ContractSearchResponse(
                contract=_contract_response(value.contract),
                derivative_security_types=list(value.derivative_security_types),
            )
            for value in values
        ]

    @app.get("/v1/options/chains", response_model=list[OptionChainResponse])
    async def option_chains(
        request: Annotated[OptionChainsRequest, Query()],
    ) -> list[OptionChainResponse]:
        policy.require_feature("option-chains")
        policy.check_option_chain(request)
        values = policy.filter_option_chains(await active_gateway.option_chains(request))
        return [
            OptionChainResponse(
                exchange=value.exchange,
                underlying_con_id=value.underlying_con_id,
                trading_class=value.trading_class,
                multiplier=value.multiplier,
                expirations=list(value.expirations),
                strikes=list(value.strikes),
            )
            for value in values
        ]

    @app.post(
        "/v1/market-data/snapshot",
        response_model=MarketDataSnapshotResponse,
        responses=TWS_ERROR_RESPONSES,
    )
    async def market_data(request: MarketDataSnapshotRequest) -> MarketDataSnapshotResponse:
        policy.require_feature("market-data")
        policy.check_contract(request.contract)
        snapshot = await active_gateway.market_data_snapshot(request)
        response = _market_data_response(request.contract, request.market_data_type, snapshot)
        await observe_snapshot(response)
        return response

    @app.get(
        "/v1/options/chain-snapshot",
        response_model=OptionChainSnapshotResponse,
        responses=TWS_ERROR_RESPONSES,
    )
    async def option_chain_snapshot(
        request: Annotated[OptionChainSnapshotRequest, Query()],
    ) -> OptionChainSnapshotResponse:
        policy.require_feature("option-chains")
        policy.require_feature("market-data")
        search = policy.filter_contract_search(
            await active_gateway.search_contracts(request.symbol)
        )
        underlyings = [
            value.contract
            for value in search
            if value.contract.symbol == request.symbol
            and value.contract.sec_type in {"STK", "IND"}
            and "OPT" in value.derivative_security_types
        ]
        underlyings.sort(key=lambda value: 0 if value.sec_type == "STK" else 1)
        if not underlyings:
            raise HTTPException(status_code=404, detail="optionable underlying was not found")
        underlying = underlyings[0]

        requested_chain = OptionChainsRequest(
            underlying_symbol=underlying.symbol,
            underlying_con_id=underlying.con_id,
            underlying_security_type=underlying.sec_type,
            exchange=request.exchange,
        )
        policy.check_option_chain(requested_chain)
        discovery_request = requested_chain.model_copy(update={"exchange": ""})
        chains = policy.filter_option_chains(await active_gateway.option_chains(discovery_request))
        matching_chains = [
            chain
            for chain in chains
            if chain.exchange == request.exchange and request.expiration in chain.expirations
        ]
        if not matching_chains:
            raise HTTPException(
                status_code=404,
                detail="option chain was not found for the requested expiration and exchange",
            )
        strikes = sorted(
            {
                strike
                for chain in matching_chains
                for strike in chain.strikes
                if (request.strike_min is None or strike >= request.strike_min)
                and (request.strike_max is None or strike <= request.strike_max)
            }
        )
        expected_contracts = len(strikes) * 2
        if expected_contracts > request.max_contracts:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"option chain contains {expected_contracts} contracts; narrow strike_min "
                    f"and strike_max or raise max_contracts up to 40"
                ),
            )

        option_spec = ContractSpec(
            symbol=request.symbol,
            sec_type="OPT",
            exchange=request.exchange,
            currency=request.currency,
            last_trade_date_or_contract_month=request.expiration,
        )
        policy.check_contract(option_spec)
        details = policy.filter_contracts(await active_gateway.contract_details(option_spec))
        strike_set = set(strikes)
        contracts = [
            contract
            for contract in details
            if contract.symbol == request.symbol
            and contract.sec_type == "OPT"
            and contract.last_trade_date_or_contract_month is not None
            and contract.last_trade_date_or_contract_month.startswith(request.expiration)
            and contract.strike in strike_set
            and contract.right in {"C", "P"}
        ]
        contracts.sort(key=lambda value: (value.strike or 0, value.right or ""))
        if not contracts:
            raise HTTPException(status_code=404, detail="no option contracts were resolved")

        if len(contracts) > request.max_contracts:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"option chain resolved to {len(contracts)} contracts; narrow strike_min "
                    f"and strike_max or raise max_contracts up to 40"
                ),
            )

        underlying_spec = _contract_spec(underlying)
        specs = [*(_contract_spec(contract) for contract in contracts), underlying_spec]
        underlying_index = len(contracts)
        collection_timeout = request.timeout_seconds or min(
            settings.tws.request_timeout_seconds,
            10,
        )
        collection = await collect_market_data_batch(
            active_gateway,
            specs,
            market_data_type=request.market_data_type,
            max_concurrency=request.max_concurrency,
            timeout_seconds=collection_timeout,
            request_timeout_seconds=settings.tws.request_timeout_seconds,
            preflight_index=len(contracts) // 2,
        )

        responses: dict[int, MarketDataSnapshotResponse] = {}
        errors: dict[int, TwsErrorDetailResponse] = {}
        for index, result in collection.results.items():
            if result.snapshot is not None:
                response = _market_data_response(
                    specs[index],
                    request.market_data_type,
                    result.snapshot,
                )
                responses[index] = response
                await observe_snapshot(response)
            else:
                observe_batch_error(result)
                assert result.error is not None
                errors[index] = _market_data_error(result.error)

        underlying_snapshot = responses.get(underlying_index)
        underlying_error = errors.get(underlying_index)
        quotes = []
        for index, contract in enumerate(contracts):
            quotes.append(
                OptionChainQuoteResponse(
                    contract=_contract_response(contract),
                    snapshot=responses.get(index),
                    error=errors.get(index),
                    attempts=collection.attempts[index],
                )
            )
        market_quote_complete = (
            underlying_snapshot is not None
            and underlying_snapshot.market_quote_complete
            and all(
                quote.snapshot is not None and quote.snapshot.market_quote_complete
                for quote in quotes
            )
        )
        model_complete = all(
            quote.snapshot is not None and quote.snapshot.model_complete is True for quote in quotes
        )
        enrichment_complete = all(
            quote.snapshot is not None and quote.snapshot.enrichment_complete for quote in quotes
        )
        reference_prices = [
            (computation.underlying_price, quote.snapshot.collected_at)
            for quote in quotes
            if quote.snapshot is not None
            and quote.snapshot.option_computations is not None
            and (computation := quote.snapshot.option_computations.model) is not None
            and computation.underlying_price is not None
            and math.isfinite(computation.underlying_price)
            and computation.underlying_price > 0
        ]
        use_reference_fallback = (
            underlying_snapshot is None or not underlying_snapshot.market_quote_complete
        )
        reference_underlying_price = (
            float(median(value for value, _ in reference_prices))
            if use_reference_fallback and reference_prices
            else None
        )
        reference_collected_at = (
            max(collected_at for _, collected_at in reference_prices)
            if reference_underlying_price is not None
            else None
        )
        return OptionChainSnapshotResponse(
            underlying=_contract_response(underlying),
            underlying_snapshot=underlying_snapshot,
            underlying_error=underlying_error,
            expiration=request.expiration,
            exchange=request.exchange,
            requested_market_data_type=request.market_data_type,
            market_quote_complete=market_quote_complete,
            quote_complete=market_quote_complete,
            model_complete=model_complete,
            enrichment_complete=enrichment_complete,
            complete=market_quote_complete and model_complete and enrichment_complete,
            collection_timeout_seconds=collection_timeout,
            max_concurrency=request.max_concurrency,
            preflight_attempted=collection.preflight_attempted,
            preflight_market_data_type=collection.preflight_market_data_type,
            preflight_seconds=collection.preflight_seconds,
            retried_contracts=collection.retried_contracts,
            collection_started_at=collection.collection_started_at,
            collected_at=collection.collected_at,
            underlying_attempts=collection.attempts[underlying_index],
            reference_underlying_price=reference_underlying_price,
            reference_underlying_price_source=(
                "option_model_median" if reference_underlying_price is not None else None
            ),
            reference_underlying_price_collected_at=reference_collected_at,
            reference_underlying_price_samples=(
                len(reference_prices) if reference_underlying_price is not None else 0
            ),
            quotes=quotes,
        )

    @app.get(
        "/v1/options/portfolio-snapshot",
        response_model=PortfolioOptionSnapshotResponse,
        responses=TWS_ERROR_RESPONSES,
    )
    async def portfolio_option_snapshot(
        request: Annotated[PortfolioOptionSnapshotRequest, Query()],
    ) -> PortfolioOptionSnapshotResponse:
        policy.require_feature("positions")
        policy.require_feature("market-data")
        positions = [
            position
            for position in policy.filter_positions(await active_gateway.positions())
            if position.contract.sec_type == "OPT" and position.position != 0
        ]
        unique_specs: list[ContractSpec] = []
        spec_indices: dict[str, int] = {}
        position_indices: list[int] = []
        for position in positions:
            spec = _contract_spec(position.contract)
            key = f"conid:{spec.con_id}" if spec.con_id is not None else spec.model_dump_json()
            index = spec_indices.get(key)
            if index is None:
                index = len(unique_specs)
                spec_indices[key] = index
                unique_specs.append(spec)
            position_indices.append(index)

        collection_timeout = request.timeout_seconds or min(
            settings.tws.request_timeout_seconds,
            10,
        )
        collection = await collect_market_data_batch(
            active_gateway,
            unique_specs,
            market_data_type=request.market_data_type,
            max_concurrency=request.max_concurrency,
            timeout_seconds=collection_timeout,
            request_timeout_seconds=settings.tws.request_timeout_seconds,
            preflight_index=(len(unique_specs) // 2 if unique_specs else None),
        )
        responses: dict[int, MarketDataSnapshotResponse] = {}
        errors: dict[int, TwsErrorDetailResponse] = {}
        for index, result in collection.results.items():
            if result.snapshot is not None:
                response = _market_data_response(
                    unique_specs[index],
                    request.market_data_type,
                    result.snapshot,
                )
                responses[index] = response
                await observe_snapshot(response)
            else:
                observe_batch_error(result)
                assert result.error is not None
                errors[index] = _market_data_error(result.error)

        quotes = [
            PortfolioOptionQuoteResponse(
                account=position.account,
                position=position.position,
                average_cost=position.average_cost,
                contract=_contract_response(position.contract),
                snapshot=responses.get(index),
                error=errors.get(index),
                attempts=collection.attempts[index],
            )
            for position, index in zip(positions, position_indices, strict=True)
        ]
        returned_count = sum(quote.snapshot is not None for quote in quotes)
        return PortfolioOptionSnapshotResponse(
            requested_market_data_type=request.market_data_type,
            position_count=len(quotes),
            returned_count=returned_count,
            coverage_percentage=(returned_count / len(quotes) * 100 if quotes else 100),
            collection_timeout_seconds=collection_timeout,
            max_concurrency=request.max_concurrency,
            retried_contracts=collection.retried_contracts,
            collection_started_at=collection.collection_started_at,
            collected_at=collection.collected_at,
            quotes=quotes,
        )

    @app.post("/v1/historical-bars", response_model=list[HistoricalBarResponse])
    async def historical_bars(request: HistoricalBarsRequest) -> list[HistoricalBarResponse]:
        policy.require_feature("historical-data")
        policy.check_contract(request.contract)
        values = await active_gateway.historical_bars(request)
        return [_historical_bar_response(value) for value in values]

    @app.post(
        "/v1/volatility/history",
        response_model=VolatilityHistoryResponse,
        responses=TWS_ERROR_RESPONSES,
    )
    async def volatility_history(request: VolatilityHistoryRequest) -> VolatilityHistoryResponse:
        policy.require_feature("historical-data")
        policy.check_contract(request.contract)
        response, _, _ = await load_volatility_history(request)
        return response

    @app.post(
        "/v1/options/volatility-comparison",
        response_model=OptionVolatilityComparisonResponse,
        responses=TWS_ERROR_RESPONSES,
    )
    async def option_volatility_comparison(
        request: OptionVolatilityComparisonRequest,
    ) -> OptionVolatilityComparisonResponse:
        policy.require_feature("market-data")
        policy.require_feature("historical-data")
        policy.check_contract(request.option_contract)
        policy.check_contract(request.underlying_contract)
        history_request = VolatilityHistoryRequest(
            contract=request.underlying_contract,
            end=request.end,
            duration=request.duration,
            bar_size=request.bar_size,
            regular_trading_hours_only=request.regular_trading_hours_only,
        )
        raw_snapshot, history_values = await asyncio.gather(
            active_gateway.market_data_snapshot(
                MarketDataSnapshotRequest(
                    contract=request.option_contract,
                    market_data_type=request.market_data_type,
                )
            ),
            load_volatility_history(history_request),
        )
        snapshot = _market_data_response(
            request.option_contract,
            request.market_data_type,
            raw_snapshot,
        )
        await observe_snapshot(snapshot)
        history, implied_values, historical_values = history_values
        return OptionVolatilityComparisonResponse(
            option_snapshot=snapshot,
            underlying_history=history,
            metrics=_volatility_comparison_metrics(
                snapshot,
                implied_values,
                historical_values,
            ),
        )

    @app.post(
        "/v1/options/observed-history",
        response_model=ObservedOptionHistoryResponse,
    )
    async def observed_option_history(
        request: ObservedOptionHistoryRequest,
    ) -> ObservedOptionHistoryResponse:
        policy.require_feature("market-data")
        policy.check_contract(request.contract)
        if not journal.enabled:
            raise HTTPException(
                status_code=409,
                detail="request-driven option history is disabled; configure history.path",
            )
        return await journal.query(request)

    return app
