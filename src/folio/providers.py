from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from .models import Record, SchemaChanged, Snapshot, money, timestamp

BOG_ACCOUNTS = "ACCOUNTS_GET_ACCOUNTS_AND_DETAILS"
WOLT_ORDERS = "/order-xp/web/v1/pages/orders"
PAGES = {
    "bog": ("https://ibank.bog.ge/accounts", "https://ibank.bog.ge/operations/"),
    "wolt": ("https://wolt.com/en/profile/order-history",),
}
ORIGINS = {
    "bog": {"https://ibank.bog.ge"},
    "wolt": {"https://wolt.com", "https://consumer-api.wolt.com"},
}
LOGIN_ORIGINS = {
    "bog": {"https://account.bog.ge", "https://ibank.bog.ge"},
    "wolt": {"https://wolt.com"},
}


def origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def resource_for(provider: str, url: str, method: str = "GET") -> str | None:
    if method != "GET" or origin(url) not in ORIGINS[provider]:
        return None
    parts = urlsplit(url)
    if provider == "wolt" and parts.path == WOLT_ORDERS:
        return "orders"
    if provider == "bog":
        if parts.path in {"/rest/rb"} and parse_qs(parts.query).get("serviceId") == [BOG_ACCOUNTS]:
            return "accounts"
        if parts.path in {"/rest/transactional/statements"}:
            if parse_qs(parts.query).get("lastOperationsFlag") == ["Y"]:
                return None
            return "transactions"
    return None


def next_url(url: str, **params: Any) -> str:
    parts = urlsplit(url)
    query = parse_qs(parts.query, keep_blank_values=True)
    query.update({key: [str(value)] for key, value in params.items()})
    return urlunsplit(parts._replace(query=urlencode(query, doseq=True)))


def bog_accounts(payload: dict) -> list[Record]:
    result = payload["result"]["accounts"]["accounts"]
    if not isinstance(result, list):
        raise SchemaChanged("BoG account response format changed.")
    records = []
    for account in result:
        ident = str(account["acctKey"])
        records.append(
            Record(
                id=ident,
                resource="accounts",
                data={
                    "name": account.get("acctName"),
                    "number": account.get("printAcctNo"),
                    "provider": "bog",
                },
            )
        )
        # A BoG account may contain several currency subaccounts. Never add them together.
        details = next(
            d for d in payload["result"]["details"] if d["mainAcctKey"] == account["mainAcctKey"]
        )
        for sub in details["subAccounts"]:
            key = str(sub["acctKey"])
            records.append(
                Record(
                    id=key,
                    resource="balances",
                    data={
                        "account_id": ident,
                        "balance": money(sub["realAmount"], sub["ccy"]),
                        "provider": "bog",
                    },
                )
            )
    return records


def bog_transactions(payload: dict) -> list[Record]:
    if not isinstance(payload["data"], list):
        raise SchemaChanged("BoG transaction response format changed.")
    records = []
    for row in payload["data"]:
        # Entry IDs distinguish separate ledger entries of the same document.
        ident = f"{row['docKey']}:{row['entryId']}"
        records.append(
            Record(
                id=ident,
                resource="transactions",
                occurred_at=timestamp(row["operationDate"]),
                data={
                    "account_id": str(row["accountKey"]),
                    "amount": money(row["amount"], row["ccy"]),
                    "description": row.get("nomination") or row.get("operationTitle"),
                    "merchant": row.get("merchantName"),
                    "status": row.get("status"),
                    "provider": "bog",
                    "source": {
                        key: row[key]
                        for key in (
                            "docKey",
                            "entryId",
                            "operationDate",
                            "docDirection",
                            "entryType",
                        )
                        if key in row
                    },
                },
            )
        )
    return records


def wolt_orders(payload: dict) -> list[Record]:
    if not isinstance(payload["orders"], list):
        raise SchemaChanged("Wolt order response format changed.")
    records = []
    for row in payload["orders"]:
        records.append(
            Record(
                id=row["purchase_id"],
                resource="orders",
                occurred_at=timestamp(row["timestamp"]),
                data={
                    "merchant": row["venue"]["name"],
                    "status": row["status"]["value"],
                    # This endpoint formats money for display; it does not expose a currency code.
                    # Preserve that text instead of guessing a currency or minor-unit exponent.
                    "display_total": row["total"],
                    "amount": None,
                    "items": [
                        {"id": item["id"], "name": item["name"], "quantity": item["count"]}
                        for item in row["items"]
                    ],
                    "provider": "wolt",
                    "source": {"timestamp": row["timestamp"]},
                },
            )
        )
    return records


def parse(provider: str, resource: str, payload: dict) -> list[Record]:
    try:
        return {
            ("bog", "accounts"): bog_accounts,
            ("bog", "transactions"): bog_transactions,
            ("wolt", "orders"): wolt_orders,
        }[provider, resource](payload)
    except (KeyError, TypeError, ValueError, StopIteration):
        raise SchemaChanged(f"{provider} {resource} response format changed.") from None


def snapshot(provider: str, records: list[Record], complete: bool, coverage: str) -> Snapshot:
    resources = ["orders"] if provider == "wolt" else ["accounts", "balances", "transactions"]
    return Snapshot(records=records, resources=resources, complete=complete, coverage=coverage)
