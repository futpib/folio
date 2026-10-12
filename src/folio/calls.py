"""One-off provider calls without collection or snapshot storage."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Annotated, Literal
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import ConnectorError

MAX_BODY = 16 * 1024 * 1024
RESERVED = {
    "timestamp",
    "recvwindow",
    "signature",
    "apikey",
    "apisecret",
    "privatekey",
    "authorization",
    "password",
    "accesstoken",
    "refreshtoken",
}
Param = Annotated[str, Field(max_length=4096)]


class CallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    endpoint: str = Field(min_length=1, max_length=4096)
    method: Literal["GET", "POST"] = "GET"
    params: list[tuple[Param, Param]] = Field(default_factory=list, max_length=100)
    body: str | None = Field(default=None, max_length=65536)
    public: bool = False

    @model_validator(mode="after")
    def validate_body(self):
        if self.body is not None:
            if self.method != "POST":
                raise ValueError("a JSON body requires POST")
            json.loads(self.body)
        return self


@dataclass
class CallResult:
    status: int
    body: str
    content_type: str | None = None


def target(request: CallRequest, origins: set[str], default: str):
    endpoint = request.endpoint
    if any(ord(c) < 33 or ord(c) == 127 for c in endpoint) or "\\" in endpoint:
        raise ValueError("endpoint must be a provider path or HTTPS URL")
    parts = urlsplit(endpoint)
    if parts.fragment or parts.username or parts.password:
        raise ValueError("endpoint must not contain user information or a fragment")
    if parts.scheme or parts.netloc:
        site = f"{parts.scheme}://{parts.netloc}"
        if site not in origins:
            raise ValueError("endpoint is outside this provider's origins")
    else:
        if not endpoint.startswith("/") or endpoint.startswith("//"):
            raise ValueError("endpoint must start with / or be a provider HTTPS URL")
        site = default
    path = parts.path or "/"
    decoded = unquote(path)
    if "\\" in decoded or any(p in {".", ".."} for p in decoded.split("/")):
        raise ValueError("endpoint must not contain path traversal")
    params = parse_qsl(parts.query, keep_blank_values=True) + request.params
    if len(params) > 100 or any(
        not key or key.lower().replace("-", "").replace("_", "") in RESERVED for key, _ in params
    ):
        raise ValueError(
            "use adapter authentication; do not supply credentials or signing parameters"
        )
    return urlunsplit((urlsplit(site).scheme, urlsplit(site).netloc, path, "", "")), params


def redact(text: str, secrets=()) -> str:
    for secret in sorted({s for s in secrets if s}, key=len, reverse=True):
        text = text.replace(secret, "<redacted>")
    return text


async def response_result(response: httpx.Response, *, secrets=()):
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > MAX_BODY:
            raise ConnectorError("Provider response exceeded the call size limit.")
    return CallResult(
        response.status_code,
        redact(body.decode(response.encoding or "utf-8", errors="replace"), secrets),
        redact(response.headers.get("content-type", "")[:256], secrets) or None,
    )
