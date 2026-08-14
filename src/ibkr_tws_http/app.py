from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, Query, Request
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
)
from .models import (
    AccountResponse,
    AccountValueResponse,
    ContractResponse,
    ContractSearchResponse,
    ContractSpec,
    HistoricalBarResponse,
    HistoricalBarsRequest,
    MarketDataSnapshotRequest,
    MarketDataSnapshotResponse,
    OptionChainResponse,
    OptionChainsRequest,
    PositionResponse,
    StatusResponse,
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
    )


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
        return JSONResponse(status_code=502, content={"detail": "TWS rejected the request"})

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

    @app.post("/v1/options/chains", response_model=list[OptionChainResponse])
    async def option_chains(request: OptionChainsRequest) -> list[OptionChainResponse]:
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

    @app.post("/v1/market-data/snapshot", response_model=MarketDataSnapshotResponse)
    async def market_data(request: MarketDataSnapshotRequest) -> MarketDataSnapshotResponse:
        policy.require_feature("market-data")
        policy.check_contract(request.contract)
        fields = await active_gateway.market_data_snapshot(request)
        return MarketDataSnapshotResponse(contract=request.contract, fields=fields)

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
