from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from . import __version__
from .config import Settings
from .gateway import (
    ContractInfo,
    Gateway,
    GatewayError,
    GatewayRequestError,
    GatewayTimeout,
    GatewayUnavailable,
    MarketDataSnapshot,
    OptionComputation,
)
from .models import (
    AccountResponse,
    AccountValueResponse,
    ContractResponse,
    ContractSearchResponse,
    ContractSpec,
    GatewayTimeoutResponse,
    HistoricalBarResponse,
    HistoricalBarsRequest,
    MarketDataSnapshotRequest,
    MarketDataSnapshotResponse,
    MarketDataType,
    OptionChainQuoteResponse,
    OptionChainResponse,
    OptionChainSnapshotRequest,
    OptionChainSnapshotResponse,
    OptionChainsRequest,
    OptionComputationResponse,
    OptionComputationsResponse,
    PositionResponse,
    StatusResponse,
    TwsErrorDetailResponse,
    TwsErrorResponse,
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
    return MarketDataSnapshotResponse(
        contract=contract,
        requested_market_data_type=requested_type,
        market_data_type=snapshot.market_data_type,
        complete=snapshot.complete,
        collected_at=snapshot.as_of,
        last_trade_time=_last_trade_time(fields),
        bid=_available_number(fields, ("bid", "delayed_bid")),
        ask=_available_number(fields, ("ask", "delayed_ask")),
        last=_available_number(fields, ("last", "delayed_last")),
        bid_size=_available_number(fields, ("bid_size", "delayed_bid_size"), nonnegative=True),
        ask_size=_available_number(fields, ("ask_size", "delayed_ask_size"), nonnegative=True),
        last_size=_available_number(fields, ("last_size", "delayed_last_size"), nonnegative=True),
        volume=_available_number(fields, volume_names, nonnegative=True),
        open_interest=_available_number(fields, open_interest_names, nonnegative=True),
        option_computations=computations,
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


TWS_ERROR_RESPONSES = {
    502: {"model": TwsErrorResponse},
    504: {"model": GatewayTimeoutResponse},
}


def create_app(settings: Settings, gateway: Gateway | None = None) -> FastAPI:
    settings.validate()
    active_gateway = gateway or OfficialGateway(settings.tws)
    policy = ExposurePolicy(settings.exposure)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await active_gateway.start()
        try:
            yield
        finally:
            await active_gateway.stop()

    app = FastAPI(
        title="IBKR TWS HTTP",
        version=__version__,
        description="A deliberately narrow, read-only facade for a local TWS API.",
        lifespan=lifespan,
    )
    app.state.gateway = active_gateway
    app.state.settings = settings

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
        return _market_data_response(request.contract, request.market_data_type, snapshot)

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

        semaphore = asyncio.Semaphore(10)

        async def snapshot_contract(
            contract_spec: ContractSpec,
        ) -> tuple[MarketDataSnapshotResponse | None, TwsErrorDetailResponse | None]:
            snapshot_request = MarketDataSnapshotRequest(
                contract=contract_spec,
                market_data_type=request.market_data_type,
            )
            try:
                async with semaphore:
                    snapshot = await active_gateway.market_data_snapshot(snapshot_request)
            except (GatewayRequestError, GatewayTimeout) as exc:
                return None, _market_data_error(exc)
            return (
                _market_data_response(
                    contract_spec,
                    request.market_data_type,
                    snapshot,
                ),
                None,
            )

        async def quote(contract: ContractInfo) -> OptionChainQuoteResponse:
            contract_spec = _contract_spec(contract)
            snapshot, error = await snapshot_contract(contract_spec)
            return OptionChainQuoteResponse(
                contract=_contract_response(contract),
                snapshot=snapshot,
                error=error,
            )

        collection_started_at = datetime.now(UTC)
        underlying_spec = _contract_spec(underlying)
        results = await asyncio.gather(
            snapshot_contract(underlying_spec),
            *(quote(contract) for contract in contracts),
        )
        underlying_snapshot, underlying_error = results[0]
        quotes = list(results[1:])
        complete = (
            underlying_snapshot is not None
            and underlying_snapshot.complete
            and all(quote.snapshot is not None and quote.snapshot.complete for quote in quotes)
        )
        return OptionChainSnapshotResponse(
            underlying=_contract_response(underlying),
            underlying_snapshot=underlying_snapshot,
            underlying_error=underlying_error,
            expiration=request.expiration,
            exchange=request.exchange,
            requested_market_data_type=request.market_data_type,
            complete=complete,
            collection_started_at=collection_started_at,
            collected_at=datetime.now(UTC),
            quotes=quotes,
        )

    @app.post("/v1/historical-bars", response_model=list[HistoricalBarResponse])
    async def historical_bars(request: HistoricalBarsRequest) -> list[HistoricalBarResponse]:
        policy.require_feature("historical-data")
        policy.check_contract(request.contract)
        values = await active_gateway.historical_bars(request)
        return [
            HistoricalBarResponse(
                time=value.time,
                open=value.open,
                high=value.high,
                low=value.low,
                close=value.close,
                volume=value.volume,
                weighted_average_price=value.weighted_average_price,
                trade_count=value.trade_count,
            )
            for value in values
        ]

    return app
