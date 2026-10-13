"""Named, single-page provider reads; callers own pagination and interpretation."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .binance import ACCOUNT_TYPES, DAY, endpoint
from .calls import CallRequest
from .models import SchemaChanged
from .providers import BOG_ACCOUNTS, WOLT_ORDERS

AccountMode = Literal["auto", "classic", "portfolio", "portfolio_pro", "portfolio_pro_span"]


class LiveReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(default=100, ge=1, le=1000)
    market: Literal["spot", "usdm", "coinm", "portfolio"] | None = None
    symbol: str | None = Field(default=None, pattern=r"^[A-Z0-9_]{1,40}$")
    since: datetime | None = None
    until: datetime | None = None
    from_id: int | None = Field(default=None, ge=0, le=9223372036854775807)
    page: int | None = Field(default=None, ge=1)
    cursor: str | None = Field(default=None, max_length=4096)
    account_mode: AccountMode = "auto"

    @model_validator(mode="after")
    def validate_range(self):
        for key in ("since", "until"):
            if value := getattr(self, key):
                value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
                setattr(self, key, value)
        if self.since and self.until and self.since >= self.until:
            raise ValueError("--until must be after --since (the upper bound is exclusive)")
        if self.from_id is not None and (self.since or self.until):
            raise ValueError("--from-id cannot be combined with date bounds")
        return self


def payload(result):
    try:
        return json.loads(result.body, parse_float=str)
    except ValueError:
        raise SchemaChanged("Provider did not return JSON for the live resource.") from None


def failure(result, body, *, purpose=None):
    bad_code = isinstance(body, dict) and type(body.get("code")) is int and body["code"] < 0
    if not 200 <= result.status < 300 or bad_code:
        return {
            "status": result.status,
            "ok": False,
            "error": body,
            **({"purpose": purpose} if purpose else {}),
            "items": [],
            "next_cursor": None,
        }
    return None


def page(result, body, items, *, next_cursor=None, limit=None, **scope):
    if not isinstance(items, list):
        raise SchemaChanged("Provider returned an unexpected live resource format.")
    return {
        "source": "live",
        "status": result.status,
        "ok": True,
        "observed_at": time.time(),
        "items": items,
        "next_cursor": next_cursor,
        "page_full": len(items) == limit if limit is not None else None,
        "scope": scope,
    }


async def read_live(connector, connection, resource, spec, *, mode_hint=None):
    if resource not in connection.resources:
        raise ValueError("resource is not supported by this connection")
    if connection.provider == "binance":
        return await binance_read(connector, connection, resource, spec, mode_hint=mode_hint)
    if any(
        (
            spec.market,
            spec.symbol,
            spec.from_id is not None,
            spec.page is not None,
            spec.account_mode != "auto",
        )
    ):
        raise ValueError("market, symbol, from-id, page and account-mode are Binance filters")
    if connection.provider == "ibkr":
        if spec.since or spec.until or spec.cursor:
            raise ValueError("IBKR live snapshots do not support history filters")
        if spec.limit != 100:
            raise ValueError("IBKR snapshots do not expose a provider page-size filter")
        route = {"accounts": "accounts", "balances": "account-summary", "positions": "positions"}[
            resource
        ]
        result = await connector.call(CallRequest(endpoint=f"/v1/{route}"))
        body = payload(result)
        return failure(result, body) or page(result, body, body)
    return await browser_read(connector, connection, resource, spec)


async def binance_read(connector, connection, resource, spec, *, mode_hint=None):
    market = spec.market
    if market is None:
        raise ValueError("Binance live reads require --market spot, usdm, coinm or portfolio")
    if spec.cursor is not None:
        raise ValueError("Binance uses --from-id for trades or --page for income")
    if market == "spot" and resource not in {"accounts", "balances", "trades"}:
        raise ValueError("Spot supports live accounts, balances and trades")
    if market == "portfolio" and resource not in {"accounts", "balances"}:
        raise ValueError("Unified Portfolio supports live accounts and balances")
    if resource == "trades" and not spec.symbol:
        raise ValueError("live trades require --symbol")
    if resource != "trades" and spec.from_id is not None:
        raise ValueError("--from-id applies to trades")
    if resource != "income" and spec.page is not None:
        raise ValueError("--page applies to income")
    if resource not in {"trades", "income"} and (spec.since or spec.until):
        raise ValueError("date bounds apply to trades and income")
    if resource not in {"trades", "income"} and spec.limit != 100:
        raise ValueError("Binance snapshots do not expose a provider page-size filter")
    if resource in {"trades", "income"} and bool(spec.since) != bool(spec.until):
        raise ValueError("provide both --since and --until for a fixed history window")
    if resource == "trades" and spec.since:
        maximum = DAY if market == "spot" else 7 * DAY
        if (spec.until - spec.since).total_seconds() * 1000 > maximum:
            raise ValueError("trade windows are at most one day for Spot or seven days for Futures")
    if spec.symbol and (resource not in {"trades", "positions", "income"} or market == "portfolio"):
        raise ValueError("--symbol applies to trades, positions and income")
    mode = (
        spec.account_mode
        if spec.account_mode != "auto"
        else connection.binance.account_mode
        if connection.binance
        else "auto"
    )
    params = {}
    if spec.symbol:
        params["symbol"] = spec.symbol
    if spec.since:
        params.update(
            startTime=int(spec.since.timestamp() * 1000),
            endTime=int(spec.until.timestamp() * 1000) - 1,
        )
    if resource in {"trades", "income"}:
        params["limit"] = spec.limit
    if resource == "trades" and not spec.since:
        params["fromId"] = spec.from_id if spec.from_id is not None else 0
    if resource == "income":
        params["page"] = spec.page or 1
    operation = {
        "accounts": "account",
        "balances": "balances" if market == "portfolio" else "account",
    }.get(resource, resource)

    mode_error = None

    async def single(client):
        nonlocal mode, mode_error
        if mode == "auto":
            if market == "spot":
                mode = "classic"
            elif (
                mode_hint
                and hashlib.sha256(connector.credentials.key.encode()).hexdigest() == mode_hint[0]
            ):
                mode = mode_hint[1]
            else:
                probe = await connector.call(
                    CallRequest(endpoint=endpoint("portfolio_pro", "account")), client=client
                )
                metadata = payload(probe)
                if isinstance(metadata, dict) and metadata.get("code") == -21001:
                    mode = "classic"
                elif error := failure(probe, metadata, purpose="account_mode"):
                    error["hint"] = (
                        "Set --account-mode to select the known mode without a metadata lookup."
                    )
                    mode_error = error
                    return probe
                else:
                    try:
                        mode = ACCOUNT_TYPES[metadata["accountType"]]
                    except (KeyError, TypeError):
                        raise SchemaChanged("Binance returned an unknown account mode.") from None
        if market == "portfolio" and mode == "classic":
            raise ValueError("this account has no unified Portfolio market")
        api_market = (
            ("portfolio" if mode == "portfolio" else "portfolio_pro")
            if market == "portfolio"
            else market
        )
        selected = (
            "span_account"
            if market == "portfolio" and mode == "portfolio_pro_span" and resource == "accounts"
            else operation
        )
        result = await connector.call(
            CallRequest(
                endpoint=endpoint(
                    api_market, selected, "portfolio" if mode == "portfolio" else "classic"
                ),
                params=[(k, str(v)) for k, v in params.items()],
            ),
            client=client,
        )
        return result

    # Keep the credential challenge open until the requested read succeeds or fails.
    result = await connector.authenticated(single)
    if mode_error is not None:
        return mode_error
    body = payload(result)
    if error := failure(result, body):
        return error
    if resource == "balances" and market != "portfolio":
        try:
            items = body["balances" if market == "spot" else "assets"]
        except (KeyError, TypeError):
            raise SchemaChanged("Binance balance response format changed.") from None
    else:
        items = [body] if resource == "accounts" else body
    next_cursor = None
    if resource == "trades" and items and "fromId" in params:
        try:
            next_cursor = {"from_id": max(int(row["id"]) for row in items) + 1}
        except (KeyError, TypeError, ValueError):
            raise SchemaChanged("Binance trade cursor is missing or invalid.") from None
    elif resource == "income" and isinstance(items, list) and len(items) == spec.limit:
        next_cursor = {"page": params["page"] + 1}
    return page(
        result,
        body,
        items,
        next_cursor=next_cursor,
        limit=spec.limit if resource in {"trades", "income"} else None,
        market=market,
        account_mode=mode,
        filters={k: v for k, v in params.items() if k != "limit"},
        pagination="trade_id"
        if "fromId" in params
        else "time_window"
        if resource == "trades"
        else "page"
        if resource == "income"
        else "snapshot",
    )


async def browser_read(connector, connection, resource, spec):
    provider = connection.provider
    if provider == "wolt":
        if spec.since or spec.until:
            raise ValueError("Wolt does not expose date-bound filters on this history endpoint")
        if spec.limit != 100:
            raise ValueError("Wolt controls its native page size")
        endpoint_path, params = WOLT_ORDERS, {}
        if spec.cursor:
            params["page_token"] = spec.cursor
    elif resource in {"accounts", "balances"}:
        if spec.since or spec.until or spec.cursor:
            raise ValueError("account snapshots do not support history filters")
        if spec.limit != 100:
            raise ValueError("account snapshots do not expose a provider page-size filter")
        endpoint_path, params = "/rest/rb", {"serviceId": BOG_ACCOUNTS}
    else:
        endpoint_path, params = "/rest/transactional/statements", {"limit": str(spec.limit)}
        if spec.cursor:
            params["searchAndSort"] = spec.cursor
        for name, key in (
            ("since", "operationDateTimeLowerBound"),
            ("until", "operationDateTimeUpperBound"),
        ):
            if value := getattr(spec, name):
                params[key] = value.isoformat()
    result = await connector.call(CallRequest(endpoint=endpoint_path, params=list(params.items())))
    body = payload(result)
    if error := failure(result, body):
        return error
    if provider == "bog" and isinstance(body, dict) and body.get("code") == 20:
        return {
            "status": result.status,
            "ok": False,
            "error": body,
            "items": [],
            "next_cursor": None,
        }
    try:
        items = (
            body["orders"]
            if provider == "wolt"
            else [body]
            if resource in {"accounts", "balances"}
            else body["data"]
        )
        cursor = (
            body.get("next_page_token")
            if provider == "wolt"
            else ",".join(map(str, items[-1]["sort"]))
            if resource == "transactions" and len(items) == spec.limit
            else None
        )
    except (KeyError, TypeError, IndexError):
        raise SchemaChanged("Browser provider live response format changed.") from None
    return page(
        result,
        body,
        items,
        next_cursor={"cursor": cursor} if cursor else None,
        limit=spec.limit if resource == "transactions" else None,
    )
