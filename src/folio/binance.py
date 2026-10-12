"""Fixed Binance read endpoints with local credentials and Zond fallback."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

import httpx

from .binance_keys import Credentials as Credentials
from .binance_keys import PassphraseRequired, private_pem, read_binance_credentials
from .config import BinanceSettings, Connection
from .credentials import CredentialFileError
from .models import ConnectorError, LoginRequired, RateLimited, Record, SchemaChanged, Snapshot
from .observability import RequestLog

DAY = 86_400_000
PAGE_SIZE = 1000
ORIGINS = {
    "spot": "https://api.binance.com",
    "usdm": "https://fapi.binance.com",
    "coinm": "https://dapi.binance.com",
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
}


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


def account_records(market, payload):
    result = [record("accounts", market, market, {"name": market})]
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
                        **{
                            key: decimal(row[key])
                            for key in (
                                "walletBalance",
                                "unrealizedProfit",
                                "marginBalance",
                                "availableBalance",
                            )
                        },
                    },
                )
            )
    return result


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

    async def request(self, client, market, operation, **params):
        # Callers cannot provide a URL, HTTP method, path, headers or signing parameters.
        url = ORIGINS[market] + ENDPOINTS[market][operation]
        await asyncio.sleep(max(0, self.last_request + self.pace - time.monotonic()))
        headers = {}
        if operation != "time":
            server, observed = self.clocks[market]
            params.update(
                timestamp=server + int((time.monotonic() - observed) * 1000), recvWindow=5000
            )
            query = urlencode(params)
            signature = self.credentials.sign(query)
            url += f"?{query}&{urlencode({'signature': signature})}"
            headers["X-MBX-APIKEY"] = self.credentials.key
        self.last_request = time.monotonic()
        log = RequestLog("GET", ORIGINS[market] + ENDPOINTS[market][operation])
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
                context = (
                    f"Binance {market} {operation} ({ENDPOINTS[market][operation]}, "
                    f"HTTP {response.status_code}"
                    + (f", code {code}" if code is not None else "")
                    + "): "
                )
                if response.status_code == 401 or code in {-2014, -2015, -1022}:
                    self.credentials = None
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
                    }.get(code, "Unauthorized request. Check API key access on Binance.")
                    raise LoginRequired(
                        context
                        + reason
                        + " After correcting it, retry 'folio sync "
                        + self.connection.id
                        + "'. Local credentials are reread; "
                        "Zond asks only for missing values."
                    )
                if code == -1021:
                    raise ConnectorError(
                        context + "INVALID_TIMESTAMP: Retry sync to refresh Binance server time."
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

    async def collect(self, client):
        # Spot's UID binds the collected history to one account even after a key rotation.
        await self.time(client, "spot")
        spot = await self.request(client, "spot", "account")
        owner = ident(spot["uid"])
        result = account_records("spot", spot) if "spot" in self.options.markets else []
        ends = {"spot": self.clocks["spot"][0]}
        for market in self.options.markets:
            if market == "spot":
                continue
            ends[market] = await self.time(client, market)
            result.extend(account_records(market, await self.request(client, market, "account")))
            result.extend(position_records(market, await self.request(client, market, "positions")))
        budget = self.connection.max_pages
        complete = True
        coverage = []
        for market in self.options.markets:
            end = ends[market]
            start = end - self.options.history_days * DAY + 1
            symbols = getattr(self.options, f"{market}_symbols")
            coverage.append(
                f"{market}: {iso(start)} through {iso(end)}; trade symbols={list(symbols)}"
            )
            for symbol in symbols:
                # Time windows work for all three APIs and do not mix fromId with time filters.
                # Split full windows to avoid skipping trades sharing the same millisecond.
                step = DAY if market == "spot" else 7 * DAY
                windows = [(a, min(a + step - 1, end)) for a in range(start, end + 1, step)]
                while windows and budget:
                    low, high = windows.pop()
                    page = rows(
                        await self.request(
                            client,
                            market,
                            "trades",
                            symbol=symbol,
                            startTime=low,
                            endTime=high,
                            limit=PAGE_SIZE,
                        )
                    )
                    budget -= 1
                    for row in page:
                        if not low <= millis(row["time"]) <= high:
                            raise SchemaChanged(
                                "Binance returned trades outside the requested range."
                            )
                        result.append(trade_record(market, symbol, row))
                    if len(page) >= PAGE_SIZE:
                        if low == high:
                            complete = False
                        else:
                            middle = (low + high) // 2
                            windows.extend([(low, middle), (middle + 1, high)])
                complete &= not windows
            if market != "spot":
                page_number, seen = 1, set()
                while budget:
                    page = rows(
                        await self.request(
                            client,
                            market,
                            "income",
                            startTime=start,
                            endTime=end,
                            page=page_number,
                            limit=PAGE_SIZE,
                        )
                    )
                    budget -= 1
                    added = []
                    for row in page:
                        if not start <= millis(row["time"]) <= end:
                            raise SchemaChanged(
                                "Binance returned income outside the requested range."
                            )
                        added.append(income_record(market, row))
                    keys = {r.id for r in added}
                    if page and not keys - seen:
                        raise SchemaChanged("Binance income pagination did not advance.")
                    seen.update(keys)
                    result.extend(added)
                    if len(page) < PAGE_SIZE:
                        break
                    page_number += 1
                else:
                    complete = False
        unique = {(r.resource, r.id): r for r in result}
        return Snapshot(
            records=list(unique.values()),
            resources=list(self.connection.resources),
            complete=complete,
            coverage=" | ".join(coverage)
            + "; current balances/positions; configured markets only; "
            "empty symbol lists skip trades; "
            + ("range finished" if complete else "page limit or dense timestamp: incomplete"),
            account_identity=f"binance:{owner}",
        )

    async def sync(self, since):
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
                                ", ".join(
                                    ORIGINS[m]
                                    for m in dict.fromkeys(("spot", *self.options.markets))
                                ),
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
                                    f"retry 'folio sync {self.connection.id}'."
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
                result = await self.collect(client)
            if challenge:
                await self.broker.result(challenge, "succeeded")
                resolved = True
            return result
        except asyncio.CancelledError:
            outcome = "cancelled"
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
