"""Binance collection and one-off read calls with local credentials and Zond fallback."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import quote, urlencode

import httpx

from .binance_keys import Credentials as Credentials
from .binance_keys import PassphraseRequired, private_pem, read_binance_credentials
from .calls import CallRequest, CallResult, response_result, target
from .config import BinanceSettings, Connection
from .credentials import CredentialFileError
from .models import (
    ConnectorError,
    LoginRequired,
    RateLimited,
    Record,
    SchemaChanged,
    Snapshot,
    SnapshotScope,
)
from .observability import RequestLog, emit

DAY = 86_400_000
PAGE_SIZE = 1000
ORIGINS = {
    "spot": "https://api.binance.com",
    "usdm": "https://fapi.binance.com",
    "coinm": "https://dapi.binance.com",
    "portfolio": "https://papi.binance.com",
    "portfolio_pro": "https://api.binance.com",
}
ENDPOINTS = {
    "spot": {
        "time": "/api/v3/time",
        "account": "/api/v3/account",
        "trades": "/api/v3/myTrades",
    },
    "usdm": {
        "time": "/fapi/v1/time",
        "account": "/fapi/v3/account",
        "positions": "/fapi/v3/positionRisk",
        "trades": "/fapi/v1/userTrades",
        "income": "/fapi/v1/income",
    },
    "coinm": {
        "time": "/dapi/v1/time",
        "account": "/dapi/v1/account",
        "positions": "/dapi/v1/positionRisk",
        "trades": "/dapi/v1/userTrades",
        "income": "/dapi/v1/income",
    },
    "portfolio": {"account": "/papi/v1/account", "balances": "/papi/v1/balance"},
    "portfolio_pro": {
        "account": "/sapi/v1/portfolio/account",
        "span_account": "/sapi/v2/portfolio/account",
        "balances": "/sapi/v1/portfolio/balance",
    },
}
PORTFOLIO_ENDPOINTS = {
    market: {
        "account": f"/papi/v1/{prefix}/account",
        "positions": f"/papi/v1/{prefix}/positionRisk",
        "trades": f"/papi/v1/{prefix}/userTrades",
        "income": f"/papi/v1/{prefix}/income",
    }
    for market, prefix in (("usdm", "um"), ("coinm", "cm"))
}
ACCOUNT_TYPES = {"PM_1": "portfolio_pro", "PM_2": "portfolio", "PM_3": "portfolio_pro_span"}


def endpoint(market, operation, api_mode="classic"):
    if api_mode == "portfolio" and market in PORTFOLIO_ENDPOINTS and operation != "time":
        return ORIGINS["portfolio"] + PORTFOLIO_ENDPOINTS[market][operation]
    return ORIGINS[market] + ENDPOINTS[market][operation]


def credential_origins(options):
    origins = {ORIGINS[market] for market in ("spot", *options.markets)}
    if options.account_mode in {"auto", "portfolio"} and set(options.markets) - {"spot"}:
        origins.add(ORIGINS["portfolio"])
    return origins


class AccessDenied(LoginRequired):
    """An endpoint rejection that may reflect a missing permission."""


class ModeUnavailable(ConnectorError):
    """The account is not enrolled in the requested account mode."""


def decimal(value) -> str:
    if not isinstance(value, str) or len(value) > 128:
        raise SchemaChanged("Binance returned an invalid decimal amount.")
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        raise SchemaChanged("Binance returned an invalid decimal amount.") from None
    if not parsed.is_finite():
        raise SchemaChanged("Binance returned an invalid decimal amount.")
    return value


def text(value, *, empty=False) -> str:
    if not isinstance(value, str) or len(value) > 128 or (not value and not empty):
        raise SchemaChanged("Binance returned an invalid identifier.")
    return value


def ident(value) -> str:
    if type(value) is int and value >= 0:
        return str(value)
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 40:
        return value
    raise SchemaChanged("Binance returned an invalid record ID.")


def millis(value) -> int:
    if type(value) is not int or not 0 <= value <= 253402300799000:
        raise SchemaChanged("Binance returned an invalid timestamp.")
    return value


def iso(value) -> str:
    return datetime.fromtimestamp(millis(value) / 1000, UTC).isoformat()


def rows(value) -> list:
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise SchemaChanged("Binance response format changed.")
    return value


def record(resource, key, market, data, occurred_at=None):
    return Record(
        id=f"{market}:{key}",
        resource=resource,
        occurred_at=occurred_at,
        data={"provider": "binance", "market": market, **data},
    )


def account_records(market, payload, api_mode="classic"):
    result = [record("accounts", market, market, {"name": market, "account_mode": api_mode})]
    if market == "spot":
        for row in rows(payload["balances"]):
            asset = text(row["asset"])
            result.append(
                record(
                    "balances",
                    asset,
                    market,
                    {
                        "asset": asset,
                        "free": decimal(row["free"]),
                        "locked": decimal(row["locked"]),
                    },
                )
            )
    else:
        for row in rows(payload["assets"]):
            asset = text(row["asset"])
            result.append(
                record(
                    "balances",
                    asset,
                    market,
                    {
                        "asset": asset,
                        "account_mode": api_mode,
                        **{
                            key: decimal(row[key])
                            for key in (
                                ("crossWalletBalance", "crossUnPnl", "maintMargin", "initialMargin")
                                if api_mode == "portfolio"
                                else (
                                    "walletBalance",
                                    "unrealizedProfit",
                                    "marginBalance",
                                    "availableBalance",
                                )
                            )
                        },
                    },
                )
            )
    return result


def portfolio_account(payload, mode):
    return record(
        "accounts",
        "portfolio",
        "portfolio",
        {
            "name": "portfolio",
            "account_mode": mode,
            "equity_unit": "USD",
            "account_status": text(payload["accountStatus"]),
            **{
                key: decimal(payload[key])
                for key in (
                    "accountEquity",
                    "actualEquity",
                    "accountMaintMargin",
                    "uniMMR",
                    "marginMM",
                    "otherMM",
                )
                if key in payload and payload[key] != ""
            },
            **(
                {
                    "risk_units": [
                        {
                            "asset": text(row["asset"]),
                            "maintenance_margin_usd": decimal(row["uniMaintainUsd"]),
                        }
                        for row in rows(payload["riskUnitMMList"])
                    ]
                }
                if "riskUnitMMList" in payload
                else {}
            ),
            **({"account_type": text(payload["accountType"])} if "accountType" in payload else {}),
        },
    )


def portfolio_balances(payload, mode):
    return [
        record(
            "balances",
            text(row["asset"]),
            "portfolio",
            {
                "asset": text(row["asset"]),
                "account_mode": mode,
                "balance_scope": "unified",
                "totalWalletBalance": decimal(row["totalWalletBalance"]),
                **{
                    key: decimal(row[key])
                    for key in (
                        "crossMarginAsset",
                        "crossMarginBorrowed",
                        "crossMarginFree",
                        "crossMarginInterest",
                        "crossMarginLocked",
                        "umWalletBalance",
                        "umUnrealizedPNL",
                        "cmWalletBalance",
                        "cmUnrealizedPNL",
                        "negativeBalance",
                        "optionWalletBalance",
                        "optionEquity",
                    )
                    if key in row and row[key] != ""
                },
            },
        )
        for row in rows(payload)
    ]


def position_records(market, payload):
    result = []
    for row in rows(payload):
        quantity = decimal(row["positionAmt"])
        if Decimal(quantity) == 0:
            continue
        symbol, side = text(row["symbol"]), text(row["positionSide"])
        result.append(
            record(
                "positions",
                f"{symbol}:{side}",
                market,
                {
                    "symbol": symbol,
                    "position_side": side,
                    "quantity": quantity,
                    "quantity_unit": "contracts" if market == "coinm" else "base_asset",
                    **{
                        key: decimal(row[key])
                        for key in (
                            "entryPrice",
                            "markPrice",
                            "unRealizedProfit",
                            "liquidationPrice",
                        )
                    },
                    **{
                        key: decimal(row[key])
                        for key in (
                            "breakEvenPrice",
                            "notional",
                            "isolatedWallet",
                            "leverage",
                        )
                        if key in row
                    },
                    **({"margin_asset": text(row["marginAsset"])} if "marginAsset" in row else {}),
                },
            )
        )
    return result


def trade_record(market, symbol, row):
    if row["symbol"] != symbol:
        raise SchemaChanged("Binance returned a trade for an unexpected symbol.")
    buyer = row["isBuyer"] if market == "spot" else row["buyer"]
    if type(buyer) is not bool:
        raise SchemaChanged("Binance returned an invalid trade side.")
    return record(
        "trades",
        f"{symbol}:{ident(row['id'])}",
        market,
        {
            "symbol": symbol,
            "trade_id": ident(row["id"]),
            "order_id": ident(row["orderId"]),
            "side": "BUY" if buyer else "SELL",
            "price": decimal(row["price"]),
            "quantity": decimal(row["qty"]),
            "quantity_unit": "contracts" if market == "coinm" else "base_asset",
            "commission": {
                "amount": decimal(row["commission"]),
                "asset": text(row["commissionAsset"]),
            },
            **{
                key: decimal(row[key])
                for key in ("quoteQty", "baseQty", "realizedPnl")
                if key in row
            },
            **({"position_side": text(row["positionSide"])} if "positionSide" in row else {}),
        },
        iso(row["time"]),
    )


def income_record(market, row):
    kind = text(row["incomeType"])
    return record(
        "income",
        f"{kind}:{ident(row['tranId'])}",
        market,
        {
            "income_type": kind,
            "symbol": text(row["symbol"], empty=True),
            "amount": decimal(row["income"]),
            "asset": text(row["asset"]),
            "transaction_id": ident(row["tranId"]),
        },
        iso(row["time"]),
    )


class BinanceConnector:
    def __init__(
        self, connection: Connection, broker=None, *, transport=None, pace=1.0, env_file=None
    ):
        self.connection, self.broker = connection, broker
        self.options = connection.binance or BinanceSettings()
        self.transport, self.pace = transport, pace
        self.credentials = None
        self.env_file = env_file
        self.clocks = {}
        self.last_request = 0.0

    async def request(self, client, market, operation, *, api_mode="classic", **params):
        # Callers cannot provide a URL, HTTP method, path, headers or signing parameters.
        url = endpoint(market, operation, api_mode)
        await asyncio.sleep(max(0, self.last_request + self.pace - time.monotonic()))
        headers = {}
        if operation != "time":
            server, observed = self.clocks[
                "spot" if market in {"portfolio", "portfolio_pro"} else market
            ]
            params.update(
                timestamp=server + int((time.monotonic() - observed) * 1000), recvWindow=5000
            )
            query = urlencode(params)
            signature = self.credentials.sign(query)
            url += f"?{query}&{urlencode({'signature': signature})}"
            headers["X-MBX-APIKEY"] = self.credentials.key
        self.last_request = time.monotonic()
        log = RequestLog("GET", endpoint(market, operation, api_mode))
        status, api_code, failure = None, None, None
        try:
            async with client.stream("GET", url, headers=headers) as response:
                status = response.status_code
                if response.status_code in {418, 429}:
                    try:
                        delay = int(response.headers.get("Retry-After", "300"))
                    except ValueError:
                        delay = 300
                    error = RateLimited(delay)
                    error.retry_after = max(60, min(delay, 259200))
                    raise error
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 16 * 1024 * 1024:
                        raise SchemaChanged("Binance response exceeded the size limit.")
                if 300 <= response.status_code < 400:
                    raise ConnectorError("Binance redirected a request; no redirect was followed.")
                try:
                    payload = json.loads(body)
                except ValueError:
                    raise ConnectorError("Binance returned an unreadable response.") from None
                code = payload.get("code") if isinstance(payload, dict) else None
                if type(code) is not int or not -999999 <= code <= 999999:
                    code = None
                api_code = code
                path = httpx.URL(endpoint(market, operation, api_mode)).path
                context = (
                    f"Binance {market} {operation} ({path}, "
                    f"HTTP {response.status_code}"
                    + (f", code {code}" if code is not None else "")
                    + "): "
                )
                if response.status_code == 401 or code in {-2014, -2015, -1022, -1002}:
                    reason = {
                        -2014: (
                            "BAD_API_KEY_FMT: API-key format invalid. "
                            "Check BINANCE_API_KEY in .env.local."
                        ),
                        -2015: (
                            "REJECTED_MBX_KEY: Invalid API-key, IP, or permissions for action. "
                            "Check that the API key is active on Binance.com, its IP allowlist "
                            "includes this machine's outbound IP, and it can read this market."
                        ),
                        -1022: (
                            "INVALID_SIGNATURE: Signature for this request is not valid. "
                            "Check that the configured private key matches the public key "
                            "registered for this API key (or the matching secret for HMAC)."
                        ),
                        -1002: "UNAUTHORIZED: API key is not authorized to read this section.",
                    }.get(code, "Unauthorized request. Check API key access on Binance.")
                    error_type = LoginRequired if code in {-2014, -1022} else AccessDenied
                    raise error_type(
                        context
                        + reason
                        + " After correcting it, retry 'folio sync "
                        + self.connection.id
                        + "'. If all reads are rejected, local credentials are reread on retry; "
                        "Zond asks only for missing values."
                    )
                if code == -1021:
                    raise ConnectorError(
                        context + "INVALID_TIMESTAMP: Retry sync to refresh Binance server time."
                    )
                if code == -21001:
                    raise ModeUnavailable(
                        context + "This account is not a Portfolio Margin account."
                    )
                if not response.is_success or (isinstance(code, int) and code < 0):
                    raise ConnectorError(
                        context + "API request failed; check account access and configured symbols."
                    )
                return payload
        except httpx.HTTPError:
            failure = "network"
            # HTTP exceptions and Binance error bodies can contain signed URLs or secrets.
            raise ConnectorError(
                "Could not reach the Binance API securely; stored data is unchanged."
            ) from None
        except asyncio.CancelledError:
            failure = "cancelled"
            raise
        finally:
            log.finish(status=status, code=api_code, error_code=failure)

    async def time(self, client, market):
        payload = await self.request(client, market, "time")
        now = millis(payload["serverTime"])
        self.clocks[market] = (now, time.monotonic())
        return now

    async def account_mode(self, client):
        mode = self.options.account_mode
        if mode != "auto" or not set(self.options.markets) - {"spot"}:
            return ("classic" if mode == "auto" else mode), None, False
        try:
            account = await self.request(client, "portfolio_pro", "account")
        except ModeUnavailable:
            return "classic", None, False
        except AccessDenied:
            try:
                account = await self.request(client, "portfolio", "account")
            except (AccessDenied, ModeUnavailable):
                # A restricted key may read positions but not account metadata.
                return "classic", None, True
            return "portfolio", account, False
        kind = text(account["accountType"])
        if kind not in ACCOUNT_TYPES:
            raise SchemaChanged("Binance returned an unknown Portfolio Margin account type.")
        mode = ACCOUNT_TYPES[kind]
        return mode, account, False

    async def collect(self, client):
        # Spot's UID binds the collected history to one account even after a key rotation.
        # When Spot is denied, a key fingerprint prevents histories from crossing accounts.
        binding = hashlib.sha256(self.credentials.key.encode()).hexdigest()
        scopes, warnings, denials = {}, [], []
        api_modes = {}
        uncertain = False

        def scope(market, resource):
            return scopes.setdefault(
                (market, resource),
                SnapshotScope(market=market, resource=resource, refreshed=False, status="complete"),
            )

        async def fetch(market, operation, resources, **params):
            sections = [scope(market, resource) for resource in resources]
            selected = api_modes.get(market, "classic")
            candidates = (
                (selected, "portfolio" if selected == "classic" else "classic")
                if uncertain and market in PORTFOLIO_ENDPOINTS
                else (selected,)
            )
            errors = []
            for candidate in candidates:
                try:
                    payload = await self.request(
                        client, market, operation, api_mode=candidate, **params
                    )
                    api_modes[market] = candidate
                    break
                except (AccessDenied, ModeUnavailable) as error:
                    errors.append(error)
            else:
                denials.extend(error for error in errors if isinstance(error, AccessDenied))
                warning = " | ".join(str(error) for error in errors)
                if params.get("symbol"):
                    warning += f" Symbol: {params['symbol']}."
                warnings.append(warning)
                for section in sections:
                    section.status = "incomplete" if section.refreshed else "denied"
                emit(
                    "section_skipped",
                    market=market,
                    operation=operation,
                    endpoint=endpoint(market, operation, candidates[-1]),
                    error_code="permission_denied",
                )
                return None
            if payload is None:
                raise SchemaChanged("Binance response format changed.")
            for section in sections:
                section.refreshed = True
                if section.status == "denied":
                    section.status = "incomplete"
            return payload

        await self.time(client, "spot")
        spot = await fetch(
            "spot", "account", ("accounts", "balances") if "spot" in self.options.markets else ()
        )
        owner = ident(spot["uid"]) if spot is not None else None
        result = (
            account_records("spot", spot)
            if spot is not None and "spot" in self.options.markets
            else []
        )
        mode, metadata, uncertain = await self.account_mode(client)
        emit(
            "account_mode_detected",
            account_mode=mode,
            detection="endpoint_fallback"
            if uncertain
            else "metadata"
            if metadata
            else "configured",
        )
        if uncertain:
            warnings.append(
                "Binance account mode metadata is inaccessible; Classic and Portfolio Margin "
                "read endpoints are tried independently. Unified account data may be stale."
            )
            for resource in ("accounts", "balances"):
                scope("portfolio", resource).status = "denied"
        if mode != "classic":
            aggregate_api = "portfolio" if mode == "portfolio" else "portfolio_pro"
            operation = "span_account" if mode == "portfolio_pro_span" else "account"
            if metadata is None or mode == "portfolio_pro_span":
                specific = await fetch(aggregate_api, operation, ())
                if specific is not None:
                    metadata = specific
                elif metadata is not None:
                    scope("portfolio", "accounts").status = "incomplete"
            if metadata is not None:
                section = scope("portfolio", "accounts")
                section.refreshed = True
                result.append(portfolio_account(metadata, mode))
            else:
                scope("portfolio", "accounts").status = "denied"
            balances = await fetch(aggregate_api, "balances", ())
            if balances is not None:
                section = scope("portfolio", "balances")
                section.refreshed = True
                result.extend(portfolio_balances(balances, mode))
            else:
                scope("portfolio", "balances").status = "denied"
        ends = {"spot": self.clocks["spot"][0]}
        for market in self.options.markets:
            if market == "spot":
                continue
            api_modes[market] = "portfolio" if mode == "portfolio" else "classic"
            ends[market] = await self.time(client, market)
            account = await fetch(market, "account", ("accounts", "balances"))
            if account is not None:
                result.extend(
                    account_records(market, account, api_modes[market] if uncertain else mode)
                )
            positions = await fetch(market, "positions", ("positions",))
            if positions is not None:
                positions = position_records(market, positions)
                for position in positions:
                    position.data["account_mode"] = mode if not uncertain else api_modes[market]
                result.extend(positions)
        if uncertain:
            # Unified balances can remain readable even when both metadata probes are denied.
            for aggregate_api in ("portfolio", "portfolio_pro"):
                try:
                    balances = await self.request(client, aggregate_api, "balances")
                except (AccessDenied, ModeUnavailable):
                    continue
                section = scope("portfolio", "balances")
                section.refreshed, section.status = True, "complete"
                result.extend(portfolio_balances(balances, "auto"))
                break
        budget = self.connection.max_pages
        coverage = []
        for market in self.options.markets:
            end = ends[market]
            start = end - self.options.history_days * DAY + 1
            symbols = getattr(self.options, f"{market}_symbols")
            coverage.append(
                f"{market}: {iso(start)} through {iso(end)}; trade symbols={list(symbols)}"
            )
            trades = scope(market, "trades")
            for symbol in symbols:
                # Time windows work for all three APIs and do not mix fromId with time filters.
                # Split full windows to avoid skipping trades sharing the same millisecond.
                step = DAY if market == "spot" else 7 * DAY
                windows = [(a, min(a + step - 1, end)) for a in range(start, end + 1, step)]
                while windows and budget:
                    low, high = windows.pop()
                    budget -= 1
                    payload = await fetch(
                        market,
                        "trades",
                        ("trades",),
                        symbol=symbol,
                        startTime=low,
                        endTime=high,
                        limit=PAGE_SIZE,
                    )
                    if payload is None:
                        break
                    page = rows(payload)
                    for row in page:
                        if not low <= millis(row["time"]) <= high:
                            raise SchemaChanged(
                                "Binance returned trades outside the requested range."
                            )
                        result.append(trade_record(market, symbol, row))
                        result[-1].data["account_mode"] = (
                            "classic"
                            if market == "spot"
                            else api_modes[market]
                            if uncertain
                            else mode
                        )
                    if len(page) >= PAGE_SIZE:
                        if low == high:
                            trades.status = "incomplete"
                        else:
                            middle = (low + high) // 2
                            windows.extend([(low, middle), (middle + 1, high)])
                if windows and trades.status == "complete":
                    trades.status = "incomplete"
            if market != "spot":
                incomes = scope(market, "income")
                page_number, seen = 1, set()
                while budget:
                    budget -= 1
                    payload = await fetch(
                        market,
                        "income",
                        ("income",),
                        startTime=start,
                        endTime=end,
                        page=page_number,
                        limit=PAGE_SIZE,
                    )
                    if payload is None:
                        break
                    page = rows(payload)
                    added = []
                    for row in page:
                        if not start <= millis(row["time"]) <= end:
                            raise SchemaChanged(
                                "Binance returned income outside the requested range."
                            )
                        added.append(income_record(market, row))
                        added[-1].data["account_mode"] = api_modes[market] if uncertain else mode
                    keys = {r.id for r in added}
                    if page and not keys - seen:
                        raise SchemaChanged("Binance income pagination did not advance.")
                    seen.update(keys)
                    result.extend(added)
                    if len(page) < PAGE_SIZE:
                        break
                    page_number += 1
                else:
                    incomes.status = "incomplete"
        if not any(section.refreshed for section in scopes.values()):
            if denials:
                raise denials[0]
            raise ConnectorError("Binance has no readable sections for the selected account mode.")
        complete = not warnings and all(section.status == "complete" for section in scopes.values())
        unique = {(r.resource, r.id): r for r in result}
        return Snapshot(
            records=list(unique.values()),
            resources=list(self.connection.resources),
            complete=complete,
            coverage=f"account mode={mode}"
            + (" (metadata denied; endpoint fallback)" if uncertain else "")
            + " | "
            + " | ".join(coverage)
            + "; current balances/positions; configured markets only; "
            "empty symbol lists skip trades; "
            + (
                "range finished"
                if complete
                else "incomplete: denied sections or page limit/dense timestamp"
            )
            + (" | Skipped: " + " | ".join(warnings) if warnings else ""),
            account_identity=f"binance:{owner}" if owner is not None else None,
            credential_identity=binding,
            scopes=list(scopes.values()),
            warnings=warnings,
        )

    async def call(self, request: CallRequest):
        if request.method != "GET":
            raise ValueError("Binance smoke calls support GET")
        prefix = httpx.URL(request.endpoint).path.split("/")[1:2]
        market = {
            "api": "spot",
            "sapi": "spot",
            "fapi": "usdm",
            "dapi": "coinm",
            "papi": "portfolio",
        }.get(prefix[0] if prefix else "")
        if market is None:
            raise ValueError(
                "Binance endpoint must start with /api/, /sapi/, /fapi/, /dapi/ or /papi/"
            )
        url, params = target(request, {ORIGINS[market]}, ORIGINS[market])
        public = request.public or any(
            url == endpoint(m, "time") for m in ("spot", "usdm", "coinm")
        )

        async def single(client):
            headers, secrets = {}, []
            query = list(params)
            if not public:
                server, observed = self.clocks.get(
                    "spot" if market == "portfolio" else market,
                    (int(time.time() * 1000), time.monotonic()),
                )
                query += [
                    ("timestamp", str(server + int((time.monotonic() - observed) * 1000))),
                    ("recvWindow", "5000"),
                ]
                signature = self.credentials.sign(urlencode(query))
                query.append(("signature", signature))
                headers["X-MBX-APIKEY"] = self.credentials.key
                secrets = [
                    self.credentials.key,
                    signature,
                    quote(signature, safe=""),
                    self.credentials.secret,
                ]
            await asyncio.sleep(max(0, self.last_request + self.pace - time.monotonic()))
            self.last_request = time.monotonic()
            known = {endpoint(m, op) for m, ops in ENDPOINTS.items() for op in ops}
            known |= {
                endpoint(m, op, "portfolio") for m, ops in PORTFOLIO_ENDPOINTS.items() for op in ops
            }
            with RequestLog(
                "GET", url if url in known else ORIGINS[market] + "/<smoke-call>"
            ) as log:
                try:
                    # Preserve duplicate parameter order: sign exactly the transmitted query.
                    requested = url + ("?" + urlencode(query) if query else "")
                    async with client.stream("GET", requested, headers=headers) as response:
                        result = await response_result(response, secrets=secrets)
                    try:
                        payload = json.loads(result.body)
                    except ValueError:
                        payload = None
                    code = payload.get("code") if isinstance(payload, dict) else None
                    code = code if type(code) is int else None
                    log.finish(status=result.status, code=code)
                    if public and isinstance(payload, dict) and "serverTime" in payload:
                        self.clocks[market] = (millis(payload["serverTime"]), time.monotonic())
                    if code in {-2014, -1022}:
                        self.credentials = None
                    return result
                except httpx.HTTPError:
                    raise ConnectorError("Could not reach the Binance API securely.") from None

        if not public:
            return await self.authenticated(single)
        async with httpx.AsyncClient(
            trust_env=False, follow_redirects=False, timeout=20, transport=self.transport
        ) as client:
            return await single(client)

    async def sync(self, since):
        return await self.authenticated(self.collect)

    async def authenticated(self, action):
        challenge = None
        answered = False
        resolved = False
        outcome = "failed"
        try:
            if self.credentials is None:
                try:
                    values = read_binance_credentials(self.env_file, key_type=self.options.key_type)
                except CredentialFileError as error:
                    raise LoginRequired(str(error)) from None
                try:
                    asymmetric = self.options.key_type in ("ed25519", "rsa") or (
                        self.options.key_type == "auto"
                        and ("private_key" in values or "private_key_file" in values)
                    )
                    fields = [
                        {"id": "api_key", "label": "API key", "kind": "secret"},
                        {
                            "id": "private_key" if asymmetric else "api_secret",
                            "label": "Private PEM key (use \\n for newlines)"
                            if asymmetric
                            else "Signing key: HMAC secret or PEM (use \\n for newlines)",
                            "kind": "secret",
                        },
                    ]
                    passphrase = {
                        "id": "private_key_passphrase",
                        "label": "Private key passphrase",
                        "kind": "secret",
                    }
                    pem = values.get("private_key", "")
                    if asymmetric and private_pem(pem).startswith(
                        b"-----BEGIN ENCRYPTED PRIVATE KEY-----"
                    ):
                        fields.append(passphrase)
                    while True:
                        missing = [field for field in fields if field["id"] not in values]
                        if missing:
                            if self.broker is None:
                                raise LoginRequired(
                                    "Binance credentials are missing from .env.local. "
                                    "Use 'folio credentials binance' or enable Zond authentication."
                                )
                            if challenge:
                                # Zond requires a result before opening the next credential step.
                                await self.broker.result(challenge, "succeeded")
                                challenge, answered, resolved = None, False, False
                            challenge = await self.broker.challenge(
                                "Binance API",
                                missing,
                                ORIGINS["spot"],
                            )
                            answer = await self.broker.receive()
                            if (
                                answer.get("type") != "answered"
                                or answer.get("challenge_id") != challenge
                            ):
                                raise RuntimeError("unexpected credential response")
                            supplied = answer.pop("values", None)
                            answered = supplied is not None
                            if not answered:
                                resolved = True
                                raise LoginRequired(
                                    "Binance API credentials were declined or expired; "
                                    "retry the Folio command."
                                )
                            try:
                                if any(
                                    not isinstance(supplied.get(field["id"]), str)
                                    or not supplied[field["id"]]
                                    for field in missing
                                ):
                                    raise LoginRequired(
                                        "Binance credential fields cannot be empty."
                                    )
                                values.update(
                                    {field["id"]: supplied[field["id"]] for field in missing}
                                )
                            finally:
                                supplied.clear()
                        try:
                            self.credentials = Credentials.from_values(
                                values, self.options.key_type
                            )
                            break
                        except PassphraseRequired:
                            fields = [passphrase]
                finally:
                    values.clear()
            async with httpx.AsyncClient(
                trust_env=False,
                follow_redirects=False,
                timeout=20,
                transport=self.transport,
            ) as client:
                result = await action(client)
            if challenge:
                success = not isinstance(result, CallResult) or 200 <= result.status < 300
                await self.broker.result(challenge, "succeeded" if success else "failed")
                resolved = True
            return result
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except LoginRequired:
            self.credentials = None
            raise
        except (KeyError, TypeError, ValueError, OverflowError):
            raise SchemaChanged(
                "Binance response format changed; stored data is unchanged."
            ) from None
        finally:
            if challenge and not resolved:
                if answered:
                    await self.broker.result(challenge, outcome)
                else:
                    await self.broker.withdraw(challenge)
