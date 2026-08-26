from __future__ import annotations

import argparse
import threading
from collections import defaultdict
from collections.abc import Sequence
from types import SimpleNamespace
from typing import ClassVar

import uvicorn

from .app import create_app
from .config import ServerConfig, Settings, TwsConfig
from .official import IbApiBindings, OfficialGateway


class MockWrapper:
    def __init__(self) -> None:
        pass


class MockContract:
    pass


class MockTickType:
    @staticmethod
    def toStr(value: int) -> str:
        return {
            10: "BID_OPTION",
            11: "ASK_OPTION",
            13: "MODEL_OPTION",
            83: "DELAYED_MODEL_OPTION",
        }.get(value, str(value))


def _contract(
    con_id: int,
    *,
    sec_type: str,
    strike: float | None = None,
    right: str | None = None,
) -> MockContract:
    value = MockContract()
    value.conId = con_id
    value.symbol = "XXI"
    value.secType = sec_type
    value.exchange = "SMART"
    value.primaryExchange = "NYSE" if sec_type == "STK" else ""
    value.currency = "USD"
    value.localSymbol = "XXI" if sec_type == "STK" else f"XXI MOCK {right}{strike:g}"
    value.tradingClass = "XXI"
    value.description = "TWENTY ONE CAPITAL INC-CL A"
    value.lastTradeDateOrContractMonth = "20280121" if sec_type == "OPT" else ""
    value.strike = strike or 0.0
    value.right = right or ""
    value.multiplier = "100" if sec_type == "OPT" else ""
    return value


UNDERLYING = _contract(837468789, sec_type="STK")
OPTIONS = tuple(
    _contract(
        829_400_000 + index,
        sec_type="OPT",
        strike=strike,
        right=right,
    )
    for index, (strike, right) in enumerate(
        (value for strike in (2.5, 5.0, 7.5, 10.0) for value in ((strike, "C"), (strike, "P"))),
        start=1,
    )
)


class MockTwsClient:
    """Deterministic callback-level mock of the TWS behaviors seen without subscriptions."""

    instances: ClassVar[list[MockTwsClient]] = []

    def __init__(self, wrapper: MockWrapper) -> None:
        self.wrapper = wrapper
        self.finished = threading.Event()
        self.market_data_type_id = 1
        self.delayed_requests: defaultdict[int, int] = defaultdict(int)
        self.cancelled_market_data: set[int] = set()
        type(self).instances.append(self)

    def connect(self, host: str, port: int, clientId: int) -> bool:
        self.wrapper.nextValidId(100)
        self.wrapper.managedAccounts("DU424242")
        return True

    def run(self) -> None:
        self.finished.wait()

    def disconnect(self) -> None:
        self.finished.set()

    def serverVersion(self) -> int:
        return 204

    def twsConnectionTime(self) -> str:
        return "20260826 23:00:00 UTC"

    def reqAccountSummary(self, request_id: int, group: str, tags: str) -> None:
        self.wrapper.accountSummary(
            request_id,
            "DU424242",
            "NetLiquidation",
            "100000",
            "USD",
        )
        self.wrapper.accountSummaryEnd(request_id)

    def cancelAccountSummary(self, request_id: int) -> None:
        pass

    def reqPositions(self) -> None:
        self.wrapper.position("DU424242", OPTIONS[0], "3", 4.2)
        self.wrapper.position("DU424242", OPTIONS[-1], "-2", 4.8)
        self.wrapper.positionEnd()

    def cancelPositions(self) -> None:
        pass

    def reqContractDetails(self, request_id: int, requested: MockContract) -> None:
        if requested.secType == "OPT":
            values = list(OPTIONS)
            if getattr(requested, "conId", 0):
                values = [value for value in values if value.conId == requested.conId]
            if getattr(requested, "strike", 0):
                values = [value for value in values if value.strike == requested.strike]
            if getattr(requested, "right", ""):
                values = [value for value in values if value.right == requested.right]
        else:
            values = [UNDERLYING]
        for value in values:
            self.wrapper.contractDetails(
                request_id,
                SimpleNamespace(
                    contract=value,
                    longName="TWENTY ONE CAPITAL INC-CL A",
                    minTick=0.01,
                ),
            )
        self.wrapper.contractDetailsEnd(request_id)

    def reqMatchingSymbols(self, request_id: int, pattern: str) -> None:
        descriptions = []
        if "XXI" in pattern.upper():
            descriptions.append(SimpleNamespace(contract=UNDERLYING, derivativeSecTypes=["OPT"]))
        self.wrapper.symbolSamples(request_id, descriptions)

    def reqSecDefOptParams(
        self,
        request_id: int,
        underlying_symbol: str,
        exchange: str,
        underlying_security_type: str,
        underlying_con_id: int,
    ) -> None:
        self.wrapper.securityDefinitionOptionParameter(
            request_id,
            "SMART",
            UNDERLYING.conId,
            "XXI",
            "100",
            {"20280121"},
            {2.5, 5.0, 7.5, 10.0},
        )
        self.wrapper.securityDefinitionOptionParameterEnd(request_id)

    def reqMarketDataType(self, market_data_type: int) -> None:
        self.market_data_type_id = market_data_type

    def reqMktData(
        self,
        request_id: int,
        requested: MockContract,
        generic_ticks: str,
        snapshot: bool,
        regulatory_snapshot: bool,
        options: list[object],
    ) -> None:
        if self.market_data_type_id == 1:
            self.wrapper.error(
                request_id,
                10091,
                "Part of requested market data requires additional subscription for API",
                "",
            )
            return

        self.wrapper.marketDataType(request_id, self.market_data_type_id)
        self.wrapper.error(
            request_id,
            10091,
            "Part of requested market data requires additional subscription for API",
            "",
        )
        self.wrapper.error(
            request_id,
            10167,
            "Requested market data is not subscribed. Displaying delayed market data.",
            "",
        )
        con_id = int(getattr(requested, "conId", 0) or 0)
        self.delayed_requests[con_id] += 1
        if requested.secType == "STK":
            return
        if requested.right == "P" and self.delayed_requests[con_id] == 1:
            return

        strike = float(requested.strike)
        underlying_price = 6.28
        model_iv = 0.72 + strike / 100
        model_price = max(0.25, abs(underlying_price - strike) * 0.6 + 1.1)
        self.wrapper.tickOptionComputation(
            request_id,
            10,
            0,
            model_iv - 0.08,
            0.4,
            -1,
            0,
            0.04,
            0.03,
            -0.01,
            underlying_price,
        )
        self.wrapper.tickOptionComputation(
            request_id,
            11,
            0,
            model_iv + 0.08,
            0.4,
            -1,
            0,
            0.04,
            0.03,
            -0.01,
            underlying_price,
        )
        self.wrapper.tickOptionComputation(
            request_id,
            83,
            0,
            model_iv,
            0.45 if requested.right == "C" else -0.45,
            model_price,
            0,
            0.04,
            0.03,
            -0.01,
            underlying_price,
        )

    def cancelMktData(self, request_id: int) -> None:
        self.cancelled_market_data.add(request_id)

    def reqHistoricalData(
        self,
        request_id: int,
        requested: MockContract,
        end: str,
        duration: str,
        bar_size: str,
        data_type: str,
        use_rth: int,
        format_date: int,
        keep_up_to_date: bool,
        options: list[object],
    ) -> None:
        if requested.secType == "OPT":
            code = 162 if bar_size == "1 day" else 2188
            self.wrapper.error(
                request_id,
                code,
                "Historical option data is unavailable without a recorded or subscribed source",
                "",
            )
            return
        closes = (
            (0.58, 0.61, 0.64, 0.66, 0.69)
            if data_type == "OPTION_IMPLIED_VOLATILITY"
            else (0.42, 0.44, 0.46, 0.48, 0.50)
        )
        for index, close in enumerate(closes, start=1):
            self.wrapper.historicalData(
                request_id,
                SimpleNamespace(
                    date=f"202608{index:02d}",
                    open=close - 0.01,
                    high=close + 0.01,
                    low=close - 0.02,
                    close=close,
                    volume="0",
                    wap=str(close),
                    barCount=0,
                ),
            )
        self.wrapper.historicalDataEnd(request_id, "", "")

    def cancelHistoricalData(self, request_id: int) -> None:
        pass


def mock_bindings() -> IbApiBindings:
    return IbApiBindings(MockTwsClient, MockWrapper, MockContract, MockTickType)


def create_mock_gateway(config: TwsConfig | None = None) -> OfficialGateway:
    return OfficialGateway(config or TwsConfig(), mock_bindings())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ibkr-tws-http-mock",
        description="Run the HTTP facade against deterministic no-subscription TWS callbacks.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18080)
    args = parser.parse_args(argv)
    settings = Settings(
        server=ServerConfig(host=args.host, port=args.port),
        tws=TwsConfig(request_timeout_seconds=0.25),
    )
    uvicorn.run(
        create_app(settings, gateway=create_mock_gateway(settings.tws)),
        host=args.host,
        port=args.port,
    )
    return 0
