from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class SmokeFailure(RuntimeError):
    """The live service did not satisfy a smoke-test assertion."""


class ApiClient(Protocol):
    def get(self, path: str) -> Any: ...

    def post(self, path: str, payload: Mapping[str, Any]) -> Any: ...


class UrlLibApiClient:
    def __init__(self, base_url: str, timeout: float):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def get(self, path: str) -> Any:
        return self._request(
            Request(f"{self._base_url}{path}", headers={"Accept": "application/json"})
        )

    def post(self, path: str, payload: Mapping[str, Any]) -> Any:
        body = json.dumps(payload).encode()
        return self._request(
            Request(
                f"{self._base_url}{path}",
                data=body,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                method="POST",
            )
        )

    def _request(self, request: Request) -> Any:
        try:
            with urlopen(request, timeout=self._timeout) as response:
                payload = response.read()
        except HTTPError as exc:
            detail = _http_error_detail(exc)
            raise SmokeFailure(f"{request.full_url} returned HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise SmokeFailure(f"could not reach {request.full_url}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise SmokeFailure(f"request to {request.full_url} timed out") from exc

        try:
            return json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SmokeFailure(f"{request.full_url} did not return valid JSON") from exc


@dataclass(frozen=True)
class SmokeResult:
    server_version: int | None
    account_count: int
    position_count: int
    symbol: str
    underlying_con_id: int
    chain_count: int
    expiration_count: int
    strike_count: int


def run_smoke(
    client: ApiClient,
    symbol: str,
    report: Callable[[str], None] = lambda message: None,
) -> SmokeResult:
    symbol = symbol.upper()

    health = _object(client.get("/healthz"), "/healthz")
    _expect(health.get("service") == "ok", "/healthz did not report a healthy service")
    report("HTTP service is healthy")

    status = _object(client.get("/v1/status"), "/v1/status")
    _expect(status.get("tws_connected") is True, "the service is not connected to TWS")
    server_version = status.get("server_version")
    _expect(
        server_version is None or type(server_version) is int,
        "/v1/status returned an invalid server version",
    )
    report(f"TWS is connected (server version {server_version or 'unknown'})")

    accounts = _list(client.get("/v1/accounts"), "/v1/accounts")
    _expect(accounts, "/v1/accounts returned no accounts after applying the exposure policy")
    _expect(
        all(
            isinstance(account, dict) and isinstance(account.get("account"), str)
            for account in accounts
        ),
        "/v1/accounts returned an invalid response",
    )
    report(f"Accounts request succeeded ({len(accounts)} exposed)")

    positions = _list(client.get("/v1/positions"), "/v1/positions")
    report(f"Positions request succeeded ({len(positions)} returned)")

    query = urlencode({"pattern": symbol})
    search_results = _list(
        client.get(f"/v1/contracts/search?{query}"),
        "/v1/contracts/search",
    )
    underlying = _select_underlying(search_results, symbol)
    contract = _object(underlying.get("contract"), "selected contract search result")
    con_id = contract.get("con_id")
    sec_type = contract.get("sec_type")
    _expect(type(con_id) is int and con_id > 0, "contract search returned an invalid contract ID")
    _expect(isinstance(sec_type, str) and sec_type, "contract search returned no security type")
    report(f"Resolved {symbol} {sec_type} contract {con_id}")

    detail_request = {
        "con_id": con_id,
        "symbol": symbol,
        "sec_type": sec_type,
        "exchange": contract.get("exchange") or "SMART",
        "currency": contract.get("currency") or "USD",
    }
    primary_exchange = contract.get("primary_exchange")
    if primary_exchange:
        detail_request["primary_exchange"] = primary_exchange
    details = _list(
        client.post("/v1/contracts/details", detail_request),
        "/v1/contracts/details",
    )
    _expect(
        any(isinstance(detail, dict) and detail.get("con_id") == con_id for detail in details),
        f"contract details did not resolve contract {con_id}",
    )
    report("Contract details request succeeded")

    chain_query = urlencode(
        {
            "underlying_symbol": symbol,
            "underlying_con_id": con_id,
            "underlying_security_type": sec_type,
            "exchange": "",
        }
    )
    chains = _list(
        client.get(f"/v1/options/chains?{chain_query}"),
        "/v1/options/chains",
    )
    valid_chains = [
        chain
        for value in chains
        if isinstance(value, dict)
        and (chain := value).get("underlying_con_id") == con_id
        and isinstance(chain.get("expirations"), list)
        and isinstance(chain.get("strikes"), list)
        and chain["expirations"]
        and chain["strikes"]
    ]
    _expect(valid_chains, f"no non-empty option chain was returned for {symbol}")
    expirations = {expiration for chain in valid_chains for expiration in chain["expirations"]}
    strikes = {strike for chain in valid_chains for strike in chain["strikes"]}
    report(
        f"Option chains request succeeded ({len(valid_chains)} chains, "
        f"{len(expirations)} expirations, {len(strikes)} strikes)"
    )

    return SmokeResult(
        server_version=server_version,
        account_count=len(accounts),
        position_count=len(positions),
        symbol=symbol,
        underlying_con_id=con_id,
        chain_count=len(valid_chains),
        expiration_count=len(expirations),
        strike_count=len(strikes),
    )


def _select_underlying(results: list[Any], symbol: str) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for result in results:
        if not isinstance(result, dict):
            continue
        contract = result.get("contract")
        derivatives = result.get("derivative_security_types")
        if (
            isinstance(contract, dict)
            and contract.get("symbol") == symbol
            and isinstance(derivatives, list)
            and "OPT" in derivatives
        ):
            candidates.append(result)

    preferred_types = {"STK": 0, "IND": 1}
    candidates.sort(key=lambda result: preferred_types.get(result["contract"].get("sec_type"), 2))
    _expect(candidates, f"contract search found no optionable underlying for {symbol}")
    return candidates[0]


def _object(value: Any, endpoint: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SmokeFailure(f"{endpoint} returned an invalid response")
    return value


def _list(value: Any, endpoint: str) -> list[Any]:
    if not isinstance(value, list):
        raise SmokeFailure(f"{endpoint} returned an invalid response")
    return value


def _expect(condition: Any, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def _http_error_detail(error: HTTPError) -> str:
    try:
        payload = json.loads(error.read(4096))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return error.reason
    if isinstance(payload, dict) and isinstance(payload.get("detail"), str):
        return payload["detail"]
    return error.reason


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Smoke-test a running IBKR TWS HTTP service against its real TWS connection."
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8080",
        help="running service URL (default: %(default)s)",
    )
    parser.add_argument(
        "--symbol",
        default="AAPL",
        help="optionable underlying used for contract checks (default: %(default)s)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="timeout for each HTTP request in seconds (default: %(default)s)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.timeout <= 0:
        _parser().error("--timeout must be greater than zero")
    try:
        run_smoke(UrlLibApiClient(args.base_url, args.timeout), args.symbol, print)
    except SmokeFailure as exc:
        print(f"smoke test failed: {exc}", file=sys.stderr)
        return 1
    print("smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
