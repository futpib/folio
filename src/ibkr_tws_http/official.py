from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import UTC
from decimal import Decimal
from typing import Any

from .config import TwsConfig
from .gateway import (
    AccountValue,
    ContractInfo,
    ContractSearchResult,
    GatewayRequestError,
    GatewayStatus,
    GatewayTimeout,
    GatewayUnavailable,
    HistoricalBar,
    OptionChain,
    Position,
)
from .models import (
    ContractSpec,
    HistoricalBarsRequest,
    MarketDataSnapshotRequest,
    OptionChainsRequest,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class IbApiBindings:
    client: type[Any]
    wrapper: type[Any]
    contract: type[Any]
    tick_type: type[Any]


def load_ibapi() -> IbApiBindings:
    try:
        from ibapi.client import EClient
        from ibapi.contract import Contract
        from ibapi.ticktype import TickTypeEnum
        from ibapi.wrapper import EWrapper
    except ImportError as exc:
        raise RuntimeError(
            "the official IBKR Python API is not installed; install the pythonclient "
            "included in the TWS API download"
        ) from exc
    return IbApiBindings(EClient, EWrapper, Contract, TickTypeEnum)


@dataclass(slots=True)
class _Pending:
    future: Future[Any] = field(default_factory=Future)
    items: list[Any] = field(default_factory=list)
    fields: dict[str, float | int | str | None] = field(default_factory=dict)


class OfficialGateway:
    _INFORMATIONAL_CODES = frozenset({1101, 1102, 2104, 2106, 2107, 2108, 2158})

    def __init__(self, config: TwsConfig, bindings: IbApiBindings | None = None):
        self._config = config
        self._bindings = bindings or load_ibapi()
        self._connected = threading.Event()
        self._stopping = threading.Event()
        self._accounts_ready = threading.Event()
        self._accounts: list[str] = []
        self._server_version: int | None = None
        self._connection_time: str | None = None
        self._client: Any | None = None
        self._supervisor: threading.Thread | None = None
        self._request_id = 1
        self._client_lock = threading.RLock()
        self._pending_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._positions_lock = asyncio.Lock()
        self._pending: dict[int, _Pending] = {}
        self._positions_pending: _Pending | None = None

    async def start(self) -> None:
        if self._supervisor is not None and self._supervisor.is_alive():
            return
        self._stopping.clear()
        self._supervisor = threading.Thread(
            target=self._supervise,
            name="ibkr-tws-client",
            daemon=True,
        )
        self._supervisor.start()

    async def stop(self) -> None:
        self._stopping.set()
        with self._client_lock:
            client = self._client
        if client is not None:
            try:
                client.disconnect()
            except Exception:
                logger.exception("failed to disconnect from TWS")
        supervisor = self._supervisor
        if supervisor is not None:
            await asyncio.to_thread(supervisor.join, self._config.connect_timeout_seconds)
        self._disconnect(GatewayUnavailable("gateway stopped"))

    def status(self) -> GatewayStatus:
        return GatewayStatus(
            connected=self._connected.is_set(),
            server_version=self._server_version,
            connection_time=self._connection_time,
        )

    async def accounts(self) -> list[str]:
        await self._connected_client()
        ready = await asyncio.to_thread(
            self._accounts_ready.wait, self._config.request_timeout_seconds
        )
        if not ready:
            raise GatewayTimeout("TWS did not provide managed accounts")
        with self._client_lock:
            return list(self._accounts)

    async def account_summary(self, tags: frozenset[str]) -> list[AccountValue]:
        request_id, pending, client = await self._begin_request()
        try:
            self._send(
                client,
                lambda: client.reqAccountSummary(request_id, "All", ",".join(sorted(tags))),
            )
            return await self._wait(request_id, pending)
        finally:
            self._discard(request_id, pending)
            self._cancel(client, lambda: client.cancelAccountSummary(request_id))

    async def positions(self) -> list[Position]:
        async with self._positions_lock:
            client = await self._connected_client()
            pending = _Pending()
            with self._pending_lock:
                self._positions_pending = pending
            try:
                self._send(client, client.reqPositions)
                return await self._wait_positions(pending)
            finally:
                with self._pending_lock:
                    if self._positions_pending is pending:
                        self._positions_pending = None
                self._cancel(client, client.cancelPositions)

    async def contract_details(self, contract: ContractSpec) -> list[ContractInfo]:
        request_id, pending, client = await self._begin_request()
        try:
            self._send(
                client,
                lambda: client.reqContractDetails(request_id, self._make_contract(contract)),
            )
            return await self._wait(request_id, pending)
        finally:
            self._discard(request_id, pending)

    async def search_contracts(self, pattern: str) -> list[ContractSearchResult]:
        request_id, pending, client = await self._begin_request()
        try:
            self._send(client, lambda: client.reqMatchingSymbols(request_id, pattern))
            return await self._wait(request_id, pending)
        finally:
            self._discard(request_id, pending)

    async def option_chains(self, request: OptionChainsRequest) -> list[OptionChain]:
        request_id, pending, client = await self._begin_request()
        try:
            self._send(
                client,
                lambda: client.reqSecDefOptParams(
                    request_id,
                    request.underlying_symbol,
                    request.exchange,
                    request.underlying_security_type,
                    request.underlying_con_id,
                ),
            )
            return await self._wait(request_id, pending)
        finally:
            self._discard(request_id, pending)

    async def market_data_snapshot(
        self, request: MarketDataSnapshotRequest
    ) -> dict[str, float | int | str | None]:
        request_id, pending, client = await self._begin_request()
        generic_ticks = ",".join(str(tick) for tick in request.generic_ticks)
        try:
            self._send(
                client,
                lambda: client.reqMktData(
                    request_id,
                    self._make_contract(request.contract),
                    generic_ticks,
                    True,
                    request.regulatory_snapshot,
                    [],
                ),
            )
            return await self._wait(request_id, pending)
        finally:
            self._discard(request_id, pending)
            self._cancel(client, lambda: client.cancelMktData(request_id))

    async def historical_bars(self, request: HistoricalBarsRequest) -> list[HistoricalBar]:
        request_id, pending, client = await self._begin_request()
        end = ""
        if request.end is not None:
            end = request.end.astimezone(UTC).strftime("%Y%m%d %H:%M:%S UTC")
        try:
            self._send(
                client,
                lambda: client.reqHistoricalData(
                    request_id,
                    self._make_contract(request.contract),
                    end,
                    request.duration,
                    request.bar_size,
                    request.data_type,
                    int(request.regular_trading_hours_only),
                    1,
                    False,
                    [],
                ),
            )
            return await self._wait(request_id, pending)
        finally:
            self._discard(request_id, pending)
            self._cancel(client, lambda: client.cancelHistoricalData(request_id))

    async def _connected_client(self) -> Any:
        if not self._connected.is_set():
            connected = await asyncio.to_thread(
                self._connected.wait, self._config.connect_timeout_seconds
            )
            if not connected:
                raise GatewayUnavailable("TWS is not connected")
        with self._client_lock:
            client = self._client
        if client is None or not self._connected.is_set():
            raise GatewayUnavailable("TWS is not connected")
        return client

    async def _begin_request(self) -> tuple[int, _Pending, Any]:
        client = await self._connected_client()
        with self._pending_lock:
            request_id = self._request_id
            self._request_id += 1
            pending = _Pending()
            self._pending[request_id] = pending
        return request_id, pending, client

    async def _wait(self, request_id: int, pending: _Pending) -> Any:
        try:
            wrapped = asyncio.wrap_future(pending.future)
            return await asyncio.wait_for(
                asyncio.shield(wrapped), self._config.request_timeout_seconds
            )
        except TimeoutError as exc:
            raise GatewayTimeout(f"TWS request {request_id} timed out") from exc
        finally:
            with self._pending_lock:
                if self._pending.get(request_id) is pending:
                    del self._pending[request_id]

    async def _wait_positions(self, pending: _Pending) -> list[Position]:
        try:
            wrapped = asyncio.wrap_future(pending.future)
            return await asyncio.wait_for(
                asyncio.shield(wrapped), self._config.request_timeout_seconds
            )
        except TimeoutError as exc:
            raise GatewayTimeout("TWS positions request timed out") from exc

    def _discard(self, request_id: int, pending: _Pending) -> None:
        with self._pending_lock:
            if self._pending.get(request_id) is pending:
                del self._pending[request_id]

    def _send(self, client: Any, operation: Callable[[], None]) -> None:
        with self._send_lock:
            with self._client_lock:
                if client is not self._client or not self._connected.is_set():
                    raise GatewayUnavailable("TWS connection changed")
            try:
                operation()
            except Exception as exc:
                raise GatewayRequestError("could not send request to TWS") from exc

    def _cancel(self, client: Any, operation: Callable[[], None]) -> None:
        with self._client_lock:
            current = client is self._client and self._connected.is_set()
        if not current:
            return
        try:
            with self._send_lock:
                operation()
        except Exception:
            logger.debug("failed to cancel completed TWS request", exc_info=True)

    def _supervise(self) -> None:
        while not self._stopping.is_set():
            client = self._new_client()
            with self._client_lock:
                self._client = client
                self._accounts = []
                self._accounts_ready.clear()
            try:
                client.connect(
                    self._config.host,
                    self._config.port,
                    clientId=self._config.client_id,
                )
                client.run()
            except Exception:
                if not self._stopping.is_set():
                    logger.exception("TWS connection failed")
            finally:
                try:
                    client.disconnect()
                except Exception:
                    logger.debug("failed to clean up TWS connection", exc_info=True)
                with self._client_lock:
                    if self._client is client:
                        self._client = None
                self._disconnect(GatewayUnavailable("TWS connection closed"))
            self._stopping.wait(self._config.reconnect_delay_seconds)

    def _new_client(self) -> Any:
        gateway = self
        wrapper_type = self._bindings.wrapper
        client_type = self._bindings.client

        class Client(wrapper_type, client_type):  # type: ignore[misc, valid-type]
            def __init__(self) -> None:
                wrapper_type.__init__(self)
                client_type.__init__(self, self)

            def nextValidId(self, orderId: int) -> None:
                gateway._on_connected(self)

            def managedAccounts(self, accountsList: str) -> None:
                gateway._on_managed_accounts(accountsList)

            def accountSummary(
                self, reqId: int, account: str, tag: str, value: str, currency: str
            ) -> None:
                gateway._append(reqId, AccountValue(account, tag, value, currency))

            def accountSummaryEnd(self, reqId: int) -> None:
                gateway._complete_items(reqId)

            def position(self, account: str, contract: Any, position: Any, avgCost: float) -> None:
                gateway._append_position(
                    Position(
                        account=account,
                        contract=gateway._contract_info(contract),
                        position=Decimal(str(position)),
                        average_cost=float(avgCost),
                    )
                )

            def positionEnd(self) -> None:
                gateway._complete_positions()

            def contractDetails(self, reqId: int, contractDetails: Any) -> None:
                gateway._append(
                    reqId,
                    gateway._contract_info(contractDetails.contract, contractDetails),
                )

            def bondContractDetails(self, reqId: int, contractDetails: Any) -> None:
                self.contractDetails(reqId, contractDetails)

            def contractDetailsEnd(self, reqId: int) -> None:
                gateway._complete_items(reqId)

            def symbolSamples(self, reqId: int, contractDescriptions: list[Any]) -> None:
                for description in contractDescriptions:
                    gateway._append(
                        reqId,
                        ContractSearchResult(
                            contract=gateway._contract_info(description.contract),
                            derivative_security_types=tuple(
                                str(value) for value in description.derivativeSecTypes
                            ),
                        ),
                    )
                gateway._complete_items(reqId)

            def securityDefinitionOptionParameter(
                self,
                reqId: int,
                exchange: str,
                underlyingConId: int,
                tradingClass: str,
                multiplier: str,
                expirations: set[str],
                strikes: set[float],
            ) -> None:
                gateway._append(
                    reqId,
                    OptionChain(
                        exchange=exchange,
                        underlying_con_id=underlyingConId,
                        trading_class=tradingClass,
                        multiplier=multiplier,
                        expirations=tuple(sorted(str(value) for value in expirations)),
                        strikes=tuple(sorted(float(value) for value in strikes)),
                    ),
                )

            def securityDefinitionOptionParameterEnd(self, reqId: int) -> None:
                gateway._complete_items(reqId)

            def tickPrice(self, reqId: int, tickType: int, price: float, attrib: Any) -> None:
                gateway._set_tick(reqId, tickType, float(price))

            def tickSize(self, reqId: int, tickType: int, size: Any) -> None:
                gateway._set_tick(reqId, tickType, gateway._number(size))

            def tickString(self, reqId: int, tickType: int, value: str) -> None:
                gateway._set_tick(reqId, tickType, value)

            def tickGeneric(self, reqId: int, tickType: int, value: float) -> None:
                gateway._set_tick(reqId, tickType, float(value))

            def tickSnapshotEnd(self, reqId: int) -> None:
                gateway._complete_fields(reqId)

            def historicalData(self, reqId: int, bar: Any) -> None:
                gateway._append(
                    reqId,
                    HistoricalBar(
                        time=str(bar.date),
                        open=float(bar.open),
                        high=float(bar.high),
                        low=float(bar.low),
                        close=float(bar.close),
                        volume=Decimal(str(bar.volume)),
                        weighted_average_price=Decimal(str(bar.wap)),
                        trade_count=int(bar.barCount),
                    ),
                )

            def historicalDataEnd(self, reqId: int, start: str, end: str) -> None:
                gateway._complete_items(reqId)

            def connectionClosed(self) -> None:
                gateway._disconnect(GatewayUnavailable("TWS connection closed"))

            def error(self, reqId: int, *args: Any) -> None:
                gateway._on_error(reqId, args)

        return Client()

    def _on_connected(self, client: Any) -> None:
        try:
            server_version = int(client.serverVersion())
        except Exception:
            server_version = None
        try:
            connection_time = str(client.twsConnectionTime()) or None
        except Exception:
            connection_time = None
        self._server_version = server_version
        self._connection_time = connection_time
        self._connected.set()

    def _on_managed_accounts(self, accounts: str) -> None:
        with self._client_lock:
            self._accounts = [value.strip() for value in accounts.split(",") if value.strip()]
        self._accounts_ready.set()

    def _on_error(self, request_id: Any, args: tuple[Any, ...]) -> None:
        if len(args) >= 4 or (len(args) >= 3 and isinstance(args[1], int)):
            code, message = args[1], args[2]
        elif len(args) >= 2:
            code, message = args[0], args[1]
        else:
            logger.warning("unrecognized TWS error callback: req=%r args=%r", request_id, args)
            return
        try:
            numeric_code = int(code)
        except (TypeError, ValueError):
            numeric_code = -1
        if numeric_code in self._INFORMATIONAL_CODES:
            logger.info("TWS notice %s: %s", numeric_code, message)
            return
        logger.warning("TWS error for request %s: %s: %s", request_id, numeric_code, message)
        if numeric_code in {1100, 1300}:
            self._disconnect(GatewayUnavailable("TWS connection lost"))
        if isinstance(request_id, int) and request_id >= 0:
            self._fail_request(
                request_id,
                GatewayRequestError(f"TWS error {numeric_code}: {message}"),
            )

    def _disconnect(self, error: Exception) -> None:
        self._connected.clear()
        self._accounts_ready.clear()
        self._server_version = None
        self._connection_time = None
        with self._pending_lock:
            pending = list(self._pending.values())
            self._pending.clear()
            if self._positions_pending is not None:
                pending.append(self._positions_pending)
                self._positions_pending = None
        for request in pending:
            self._set_exception(request.future, error)

    def _append(self, request_id: int, item: Any) -> None:
        with self._pending_lock:
            pending = self._pending.get(request_id)
            if pending is not None:
                pending.items.append(item)

    def _complete_items(self, request_id: int) -> None:
        with self._pending_lock:
            pending = self._pending.get(request_id)
            items = list(pending.items) if pending is not None else None
        if pending is not None:
            self._set_result(pending.future, items)

    def _set_tick(self, request_id: int, tick_type: int, value: Any) -> None:
        try:
            name = str(self._bindings.tick_type.toStr(tick_type))
        except Exception:
            name = str(tick_type)
        name = name.lower().replace(" ", "_").replace("-", "_")
        with self._pending_lock:
            pending = self._pending.get(request_id)
            if pending is not None:
                pending.fields[name] = value

    def _complete_fields(self, request_id: int) -> None:
        with self._pending_lock:
            pending = self._pending.get(request_id)
            fields = dict(pending.fields) if pending is not None else None
        if pending is not None:
            self._set_result(pending.future, fields)

    def _append_position(self, position: Position) -> None:
        with self._pending_lock:
            if self._positions_pending is not None:
                self._positions_pending.items.append(position)

    def _complete_positions(self) -> None:
        with self._pending_lock:
            pending = self._positions_pending
            items = list(pending.items) if pending is not None else None
        if pending is not None:
            self._set_result(pending.future, items)

    def _fail_request(self, request_id: int, error: Exception) -> None:
        with self._pending_lock:
            pending = self._pending.get(request_id)
        if pending is not None:
            self._set_exception(pending.future, error)

    @staticmethod
    def _set_result(future: Future[Any], result: Any) -> None:
        if not future.done():
            future.set_result(result)

    @staticmethod
    def _set_exception(future: Future[Any], error: Exception) -> None:
        if not future.done():
            future.set_exception(error)

    def _make_contract(self, spec: ContractSpec) -> Any:
        contract = self._bindings.contract()
        values = {
            "conId": spec.con_id,
            "symbol": spec.symbol,
            "secType": spec.sec_type,
            "exchange": spec.exchange,
            "primaryExchange": spec.primary_exchange,
            "currency": spec.currency,
            "lastTradeDateOrContractMonth": spec.last_trade_date_or_contract_month,
            "strike": spec.strike,
            "right": spec.right,
            "multiplier": spec.multiplier,
            "localSymbol": spec.local_symbol,
            "tradingClass": spec.trading_class,
        }
        for name, value in values.items():
            if value is not None:
                setattr(contract, name, value)
        return contract

    @staticmethod
    def _contract_info(contract: Any, details: Any | None = None) -> ContractInfo:
        primary_exchange = getattr(contract, "primaryExchange", "") or None
        local_symbol = getattr(contract, "localSymbol", "") or None
        trading_class = getattr(contract, "tradingClass", "") or None
        long_name = (
            (getattr(details, "longName", "") or None)
            if details is not None
            else (getattr(contract, "description", "") or None)
        )
        min_tick = getattr(details, "minTick", None) if details is not None else None
        return ContractInfo(
            con_id=int(getattr(contract, "conId", 0)),
            symbol=str(getattr(contract, "symbol", "")),
            sec_type=str(getattr(contract, "secType", "")),
            exchange=str(getattr(contract, "exchange", "")),
            primary_exchange=primary_exchange,
            currency=str(getattr(contract, "currency", "")),
            local_symbol=local_symbol,
            trading_class=trading_class,
            long_name=long_name,
            min_tick=float(min_tick) if min_tick is not None else None,
        )

    @staticmethod
    def _number(value: Any) -> float | int:
        number = float(value)
        return int(number) if number.is_integer() else number
