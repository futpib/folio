from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import math
import os
import sys
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlencode

from pydantic import BaseModel, ValidationError

from ibkr_tws_http.models import (
    ContractSpec,
    HistoricalBarsRequest,
    MarketDataSnapshotRequest,
    MarketDataType,
    ObservedOptionHistoryRequest,
    OptionChainSnapshotRequest,
    OptionChainsRequest,
    OptionVolatilityComparisonRequest,
    PortfolioOptionSnapshotRequest,
    VolatilityHistoryRequest,
)

MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class CliError(Exception):
    """A request failed without producing a usable API response."""


def _loopback(value: str) -> str:
    if value == "localhost":
        return value
    try:
        if ipaddress.ip_address(value).is_loopback:
            return value
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("host must be a loopback IP address or localhost")


def _positive_timeout(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be a positive finite number") from exc
    if not math.isfinite(timeout) or not 0 < timeout <= 86400:
        raise argparse.ArgumentTypeError("timeout must be greater than 0 and at most 86400 seconds")
    return timeout


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _contract_json(value: str) -> dict[str, Any]:
    try:
        return ContractSpec.model_validate_json(value).model_dump(mode="json", exclude_none=True)
    except ValidationError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _contract_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("contract (symbol, con-id, or local-symbol required)")
    for name in (
        "symbol",
        "sec-type",
        "exchange",
        "primary-exchange",
        "currency",
        "last-trade-date-or-contract-month",
        "right",
        "multiplier",
        "local-symbol",
        "trading-class",
    ):
        group.add_argument(f"--{name}", default=argparse.SUPPRESS)
    group.add_argument("--con-id", type=int, default=argparse.SUPPRESS)
    group.add_argument("--strike", type=float, default=argparse.SUPPRESS)
    parser.set_defaults(contract_fields=True)


def _market_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--market-data-type", choices=list(MarketDataType), default="auto")


def _batch_arguments(parser: argparse.ArgumentParser) -> None:
    _market_arguments(parser)
    parser.add_argument("--max-concurrency", type=int, default=10)
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=argparse.SUPPRESS,
        help="server batch collection window, 1-120 seconds (separate from HTTP timeout)",
    )


def _history_arguments(parser: argparse.ArgumentParser, *, volatility: bool) -> None:
    parser.add_argument("--end", default=argparse.SUPPRESS, help="ISO 8601 time with UTC offset")
    parser.add_argument("--duration", default="1 M" if volatility else "1 D")
    parser.add_argument("--bar-size", default="1 day" if volatility else "5 mins")
    parser.add_argument(
        "--outside-rth",
        dest="regular_trading_hours_only",
        action="store_false",
        help="include data outside regular trading hours",
    )


def build_parser() -> argparse.ArgumentParser:
    connection = argparse.ArgumentParser(add_help=False)
    connection.add_argument(
        "--host",
        type=_loopback,
        default=argparse.SUPPRESS,
        help="local HTTP service host (default: 127.0.0.1)",
    )
    connection.add_argument(
        "--port",
        type=_port,
        default=argparse.SUPPRESS,
        help="local HTTP service port (default: 8080)",
    )
    connection.add_argument(
        "--timeout",
        type=_positive_timeout,
        default=argparse.SUPPRESS,
        help="HTTP socket timeout in seconds, at most 86400 (default: 180)",
    )
    parser = argparse.ArgumentParser(
        prog="ibkr-tws",
        parents=[connection],
        description="Read local IBKR TWS HTTP data. JSON on stdout; errors on stderr. No prompts.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    def command(
        name: str,
        path: str,
        description: str,
        model: type[BaseModel] | None = None,
        *,
        method: str = "GET",
    ) -> argparse.ArgumentParser:
        sub = commands.add_parser(
            name, parents=[connection], help=description, description=description
        )
        sub.set_defaults(path=path, method=method, model=model, contract_fields=False)
        return sub

    for name, path, help_text in (
        ("health", "/healthz", "Check HTTP service health"),
        ("status", "/v1/status", "Show TWS connection status"),
        ("capabilities", "/v1/capabilities", "Show observed data availability"),
        ("accounts", "/v1/accounts", "List exposed accounts"),
        ("account-summary", "/v1/account-summary", "Read exposed account values"),
        ("positions", "/v1/positions", "Read positions"),
    ):
        command(name, path, help_text)

    search = command("contract-search", "/v1/contracts/search", "Search contract descriptions")
    search.add_argument("pattern")
    details = command(
        "contract-details",
        "/v1/contracts/details",
        "Resolve a contract",
        ContractSpec,
        method="POST",
    )
    _contract_arguments(details)

    chains = command(
        "option-chains",
        "/v1/options/chains",
        "List option expirations and strikes",
        OptionChainsRequest,
    )
    chains.add_argument("underlying_symbol")
    chains.add_argument("underlying_con_id", type=int)
    chains.add_argument("--underlying-security-type", default="STK")
    chains.add_argument("--exchange", default="")

    chain = command(
        "chain-snapshot",
        "/v1/options/chain-snapshot",
        "Collect an option chain",
        OptionChainSnapshotRequest,
    )
    chain.add_argument("symbol")
    chain.add_argument("expiration", help="expiration as YYYYMMDD")
    chain.add_argument("--exchange", default="SMART")
    chain.add_argument("--currency", default="USD")
    chain.add_argument("--strike-min", type=float, default=argparse.SUPPRESS)
    chain.add_argument("--strike-max", type=float, default=argparse.SUPPRESS)
    chain.add_argument("--max-contracts", type=int, default=40)
    _batch_arguments(chain)
    portfolio = command(
        "portfolio-snapshot",
        "/v1/options/portfolio-snapshot",
        "Collect held option quotes",
        PortfolioOptionSnapshotRequest,
    )
    _batch_arguments(portfolio)

    snapshot = command(
        "snapshot",
        "/v1/market-data/snapshot",
        "Collect a contract quote",
        MarketDataSnapshotRequest,
        method="POST",
    )
    _contract_arguments(snapshot)
    _market_arguments(snapshot)
    snapshot.add_argument("--generic-ticks", type=int, nargs="+", default=[])
    snapshot.add_argument("--regulatory-snapshot", action="store_true")

    for name, path, model, volatility in (
        ("historical-bars", "/v1/historical-bars", HistoricalBarsRequest, False),
        ("volatility-history", "/v1/volatility/history", VolatilityHistoryRequest, True),
    ):
        history = command(name, path, f"Read {name.replace('-', ' ')}", model, method="POST")
        _contract_arguments(history)
        _history_arguments(history, volatility=volatility)
        if not volatility:
            history.add_argument("--data-type", default="TRADES")

    comparison = command(
        "volatility-comparison",
        "/v1/options/volatility-comparison",
        "Compare current option IV with underlying history",
        OptionVolatilityComparisonRequest,
        method="POST",
    )
    comparison.add_argument(
        "--option-contract",
        type=_contract_json,
        required=True,
        help="ContractSpec JSON object with sec_type OPT",
    )
    comparison.add_argument(
        "--underlying-contract",
        type=_contract_json,
        required=True,
        help="ContractSpec JSON object with sec_type STK or IND",
    )
    _market_arguments(comparison)
    _history_arguments(comparison, volatility=True)

    observed = command(
        "observed-history",
        "/v1/options/observed-history",
        "Read transient exact-option observations",
        ObservedOptionHistoryRequest,
        method="POST",
    )
    _contract_arguments(observed)
    observed.add_argument(
        "--start", default=argparse.SUPPRESS, help="ISO 8601 time with UTC offset"
    )
    observed.add_argument("--end", default=argparse.SUPPRESS, help="ISO 8601 time with UTC offset")
    observed.add_argument("--limit", type=int, default=1000)
    return parser


def _payload(args: argparse.Namespace) -> dict[str, Any]:
    metadata = {"command", "path", "method", "model", "contract_fields", "host", "port", "timeout"}
    payload = {key: value for key, value in vars(args).items() if key not in metadata}
    if args.contract_fields and args.model is not ContractSpec:
        payload["contract"] = {
            field: payload.pop(field) for field in ContractSpec.model_fields if field in payload
        }
    if args.model is not None:
        payload = args.model.model_validate(payload).model_dump(mode="json", exclude_none=True)
    elif args.command == "contract-search" and not 1 <= len(args.pattern) <= 64:
        raise ValueError("pattern must contain between 1 and 64 characters")
    return payload


def request(args: argparse.Namespace, payload: dict[str, Any]) -> str:
    path = args.path
    body = None
    headers = {"Accept": "application/json"}
    if args.method == "POST":
        body = json.dumps(payload, allow_nan=False).encode()
        headers["Content-Type"] = "application/json"
    elif payload:
        path += "?" + urlencode(payload)
    host, port = getattr(args, "host", "127.0.0.1"), getattr(args, "port", 8080)
    connection = http.client.HTTPConnection(host, port, timeout=getattr(args, "timeout", 180))
    try:
        connection.request(args.method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise CliError("HTTP response exceeded the 16 MiB limit")
        try:
            text = raw.decode("utf-8")
            json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CliError(f"HTTP {response.status}: service did not return valid JSON") from exc
        if not 200 <= response.status < 300:
            raise CliError(f"HTTP {response.status}: {text.strip()}")
        return text
    except TimeoutError as exc:
        raise CliError(f"HTTP request to {host}:{port} timed out") from exc
    except (OSError, http.client.HTTPException) as exc:
        raise CliError(
            f"HTTP request to {host}:{port} failed: {exc}. "
            "Check that ibkr-tws-http is running on that loopback address."
        ) from exc
    finally:
        connection.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        payload = _payload(args)
        json.dumps(payload, allow_nan=False)
    except (ValidationError, ValueError) as exc:
        parser.error(str(exc))
    try:
        response = request(args, payload)
        print(response.rstrip("\n"), flush=True)
    except CliError as exc:
        print(f"ibkr-tws: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        with open(os.devnull, "w") as sink:
            os.dup2(sink.fileno(), sys.stdout.fileno())
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
