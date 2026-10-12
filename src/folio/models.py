from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=512)
    resource: str
    occurred_at: str | None = None
    data: dict[str, Any]


class Snapshot(BaseModel):
    records: list[Record]
    resources: list[str]
    complete: bool
    coverage: str


class ConnectorError(Exception):
    """A safe, fixed message that may be returned to an API client."""

    code = "connector_failed"
    retry_after = 300


class LoginRequired(ConnectorError):
    code = "login_required"


class SchemaChanged(ConnectorError):
    code = "schema_changed"


class RateLimited(ConnectorError):
    code = "rate_limited"

    def __init__(self, retry_after: int = 300):
        super().__init__("Provider rate limit reached; sync is paused.")
        self.retry_after = max(60, min(retry_after, 86400))


def money(value: Any, currency: Any) -> dict[str, str]:
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise SchemaChanged("Provider returned an invalid amount.") from None
    if not amount.is_finite() or not isinstance(currency, str) or len(currency) != 3:
        raise SchemaChanged("Provider returned an invalid amount or currency.")
    return {"amount": str(amount), "currency": currency.upper()}


def timestamp(value: Any) -> str | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        # An unzoned provider timestamp remains source data, not a guessed UTC time.
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(UTC).isoformat()
    except ValueError:
        return None
