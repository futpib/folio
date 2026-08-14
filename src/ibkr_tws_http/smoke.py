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

EXPECTED_API_OPERATIONS = {
    "/healthz": "get",
    "/v1/status": "get",
    "/v1/accounts": "get",
    "/v1/account-summary": "get",
    "/v1/positions": "get",
    "/v1/contracts/search": "get",
    "/v1/contracts/details": "post",
    "/v1/options/chains": "get",
    "/v1/options/chain-snapshot": "get",
    "/v1/market-data/snapshot": "post",
    "/v1/historical-bars": "post",
}


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
    api_version: str
    server_version: int | None
    account_count: int
    account_summary_count: int
    position_count: int
    symbol: str
    underlying_con_id: int
    chain_count: int
    expiration_count: int
    strike_count: int
    quoted_contract_count: int | None = None
    underlying_market_data_type: str | None = None


def run_smoke(
    client: ApiClient,
    symbol: str,
    report: Callable[[str], None] = lambda message: None,
    *,
    check_market_data: bool = True,
    expiration: str | None = None,
    market_data_type: str = "auto",
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

    openapi = _object(client.get("/openapi.json"), "/openapi.json")
    info = _object(openapi.get("info"), "/openapi.json info")
    api_version = info.get("version")
    _expect(isinstance(api_version, str) and api_version, "/openapi.json has no API version")
    paths = _object(openapi.get("paths"), "/openapi.json paths")
    for path, method in EXPECTED_API_OPERATIONS.items():
        operations = _object(paths.get(path), f"/openapi.json path {path}")
        _expect(method in operations, f"/openapi.json does not expose {method.upper()} {path}")
    report(f"OpenAPI contract is complete (service version {api_version})")

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

    account_summary = _list(client.get("/v1/account-summary"), "/v1/account-summary")
    _expect(
        all(
            isinstance(value, dict)
            and all(
                isinstance(value.get(field), str)
                for field in ("account", "tag", "value", "currency")
            )
            for value in account_summary
        ),
        "/v1/account-summary returned an invalid response",
    )
    report(f"Account summary request succeeded ({len(account_summary)} values returned)")

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

    underlying_market_data_type = None
    if check_market_data:
        underlying_snapshot = _object(
            client.post(
                "/v1/market-data/snapshot",
                {"contract": detail_request, "market_data_type": market_data_type},
            ),
            "/v1/market-data/snapshot",
        )
        underlying_market_data_type = underlying_snapshot.get("market_data_type")
        _expect(
            underlying_market_data_type in {"live", "frozen", "delayed", "delayed-frozen"},
            "/v1/market-data/snapshot returned an invalid market data type",
        )
        _expect(
            any(
                underlying_snapshot.get(field) is not None
                for field in ("bid", "ask", "last", "mark")
            ),
            f"market snapshot returned no usable price for {symbol}",
        )
        _expect(
            underlying_snapshot.get("quote_complete") is True,
            f"market snapshot returned an incomplete quote for {symbol}",
        )
        report(f"Underlying market snapshot succeeded ({underlying_market_data_type})")

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

    quoted_contract_count = None
    if check_market_data:
        smart_chains = [chain for chain in valid_chains if chain.get("exchange") == "SMART"]
        _expect(smart_chains, f"no SMART option chain was returned for {symbol}")
        selected_expiration = expiration or min(
            value for chain in smart_chains for value in chain["expirations"]
        )
        matching_chains = [
            chain for chain in smart_chains if selected_expiration in chain["expirations"]
        ]
        _expect(
            matching_chains,
            f"SMART option chain has no expiration {selected_expiration}",
        )
        selected_strikes = sorted(
            {strike for chain in matching_chains for strike in chain["strikes"]}
        )
        _expect(selected_strikes, "SMART option chain returned no strikes")
        middle = len(selected_strikes) // 2
        selected_strikes = selected_strikes[max(0, middle - 1) : middle + 2]
        quote_query = urlencode(
            {
                "symbol": symbol,
                "expiration": selected_expiration,
                "exchange": "SMART",
                "market_data_type": market_data_type,
                "strike_min": selected_strikes[0],
                "strike_max": selected_strikes[-1],
                "max_contracts": len(selected_strikes) * 2,
            }
        )
        chain_snapshot = _object(
            client.get(f"/v1/options/chain-snapshot?{quote_query}"),
            "/v1/options/chain-snapshot",
        )
        batch_underlying = _object(
            chain_snapshot.get("underlying_snapshot"),
            "/v1/options/chain-snapshot underlying_snapshot",
        )
        _expect(
            any(
                batch_underlying.get(field) is not None for field in ("bid", "ask", "last", "mark")
            ),
            f"batch option snapshot returned no usable underlying price for {symbol}",
        )
        _expect(
            isinstance(chain_snapshot.get("quote_complete"), bool)
            and isinstance(chain_snapshot.get("enrichment_complete"), bool),
            "/v1/options/chain-snapshot returned invalid completeness indicators",
        )
        quotes = _list(chain_snapshot.get("quotes"), "/v1/options/chain-snapshot quotes")
        priced_quotes = [
            quote
            for quote in quotes
            if isinstance(quote, dict)
            and isinstance(quote.get("snapshot"), dict)
            and _snapshot_has_price(quote["snapshot"])
        ]
        _expect(
            priced_quotes,
            f"batch option snapshot returned no usable prices for {symbol} {selected_expiration}",
        )
        quoted_contract_count = len(priced_quotes)
        report(
            f"Batch option snapshot succeeded "
            f"({len(priced_quotes)}/{len(quotes)} contracts priced, "
            f"requested {market_data_type})"
        )

    return SmokeResult(
        api_version=api_version,
        server_version=server_version,
        account_count=len(accounts),
        account_summary_count=len(account_summary),
        position_count=len(positions),
        symbol=symbol,
        underlying_con_id=con_id,
        chain_count=len(valid_chains),
        expiration_count=len(expirations),
        strike_count=len(strikes),
        quoted_contract_count=quoted_contract_count,
        underlying_market_data_type=underlying_market_data_type,
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


def _snapshot_has_price(snapshot: dict[str, Any]) -> bool:
    if any(snapshot.get(field) is not None for field in ("bid", "ask", "last", "mark")):
        return True
    computations = snapshot.get("option_computations")
    if not isinstance(computations, dict):
        return False
    model = computations.get("model")
    return isinstance(model, dict) and model.get("option_price") is not None


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
        default=60.0,
        help="timeout for each HTTP request in seconds (default: %(default)s)",
    )
    market_data_group = parser.add_mutually_exclusive_group()
    market_data_group.add_argument(
        "--check-market-data",
        dest="check_market_data",
        action="store_true",
        default=True,
        help="require underlying and batch option prices (default)",
    )
    market_data_group.add_argument(
        "--skip-market-data",
        dest="check_market_data",
        action="store_false",
        help="skip quote checks and test metadata endpoints only",
    )
    parser.add_argument(
        "--expiration",
        help="YYYYMMDD option expiration for quote checks (default: nearest SMART expiry)",
    )
    parser.add_argument(
        "--market-data-type",
        choices=("auto", "live", "frozen", "delayed", "delayed-frozen"),
        default="auto",
        help="quote type for --check-market-data (default: %(default)s)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.timeout <= 0:
        _parser().error("--timeout must be greater than zero")
    try:
        run_smoke(
            UrlLibApiClient(args.base_url, args.timeout),
            args.symbol,
            print,
            check_market_data=args.check_market_data,
            expiration=args.expiration,
            market_data_type=args.market_data_type,
        )
    except SmokeFailure as exc:
        print(f"smoke test failed: {exc}", file=sys.stderr)
        return 1
    print("smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
