"""Binance-shaped read API with independent signature verification and no live traffic."""

from __future__ import annotations

import base64
import hashlib
import hmac
from urllib.parse import parse_qs, unquote

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

NOW = 1_792_000_000_000
KEY = "FolioFixtureApiKey123456"
SECRET = "FolioFixtureHmacSecret123456"


def trade(symbol, ident=1, when=NOW - 100):
    return {
        "symbol": symbol,
        "id": ident,
        "orderId": 900 + ident,
        "price": "123.4567890123456789",
        "qty": "0.001",
        "quoteQty": "0.1234567890123456789",
        "baseQty": "0.00001",
        "commission": "0.000002",
        "commissionAsset": "USDT",
        "realizedPnl": "-1.25",
        "positionSide": "LONG",
        "isBuyer": True,
        "buyer": True,
        "time": when,
    }


def income(ident=1, kind="FUNDING_FEE"):
    return {
        "tranId": ident,
        "incomeType": kind,
        "income": "-0.0000123456789",
        "asset": "USDT",
        "symbol": "BTCUSDT",
        "time": NOW - 100,
    }


class Api:
    def __init__(self, public_key=None):
        self.requests = []
        self.uid = 42
        self.failure = None
        self.history = None
        self.public_key = public_key

    def handle(self, request):
        path = request.url.path
        assert request.method == "GET"
        if path.endswith("/time"):
            assert "X-MBX-APIKEY" not in request.headers
            return httpx.Response(200, json={"serverTime": NOW})
        encoded, signature = request.url.query.decode().rsplit("&signature=", 1)
        if self.public_key is None:
            expected = hmac.new(SECRET.encode(), encoded.encode(), hashlib.sha256).hexdigest()
            valid = hmac.compare_digest(expected, signature)
        else:
            try:
                raw = base64.b64decode(unquote(signature), validate=True)
                if isinstance(self.public_key, rsa.RSAPublicKey):
                    self.public_key.verify(
                        raw, encoded.encode(), padding.PKCS1v15(), hashes.SHA256()
                    )
                else:
                    self.public_key.verify(raw, encoded.encode())
                valid = True
            except (ValueError, InvalidSignature):
                valid = False
            assert all(c not in signature for c in "+/="), "base64 signatures must be URL encoded"
        if request.headers["X-MBX-APIKEY"] != KEY or not valid:
            return httpx.Response(401, json={"code": -2015, "msg": "Bad key: " + KEY + SECRET})
        params = {k: v[0] for k, v in parse_qs(encoded).items()}
        assert NOW <= int(params.pop("timestamp")) < NOW + 60_000
        assert params.pop("recvWindow") == "5000"
        self.requests.append((path, params))
        if self.failure:
            response = self.failure(request)
            if response is not None:
                return response
        if path == "/api/v3/account":
            return httpx.Response(
                200,
                json={
                    "uid": self.uid,
                    "balances": [{"asset": "USDT", "free": "123.0000000000001", "locked": "4"}],
                    "unneeded": "never persist this field",
                },
            )
        if path in {"/fapi/v3/account", "/dapi/v1/account"}:
            return httpx.Response(
                200,
                json={
                    "assets": [
                        {
                            "asset": "BTC" if path.startswith("/dapi") else "USDT",
                            "walletBalance": "42.01",
                            "unrealizedProfit": "-2.0",
                            "marginBalance": "40.01",
                            "availableBalance": "39.01",
                        }
                    ]
                },
            )
        if path in {"/fapi/v3/positionRisk", "/dapi/v1/positionRisk"}:
            return httpx.Response(
                200,
                json=[
                    {
                        "symbol": "BTCUSD_PERP" if path.startswith("/dapi") else "BTCUSDT",
                        "positionSide": side,
                        "positionAmt": amount,
                        "entryPrice": "50000.00",
                        "markPrice": "50100.00",
                        "unRealizedProfit": "-1.5",
                        "liquidationPrice": "45000.0",
                        "marginAsset": "BTC" if path.startswith("/dapi") else "USDT",
                    }
                    for side, amount in [("LONG", "0.1"), ("SHORT", "-0.2"), ("BOTH", "0")]
                ],
            )
        if path in {"/api/v3/myTrades", "/fapi/v1/userTrades", "/dapi/v1/userTrades"}:
            assert int(params["endTime"]) - int(params["startTime"]) < (
                86_400_000 if path.startswith("/api") else 7 * 86_400_000
            )
            assert "fromId" not in params
            records = self.history or [trade(params["symbol"])]
            records = [
                r
                for r in records
                if int(params["startTime"]) <= r["time"] <= int(params["endTime"])
            ]
            # Returning newest first catches pagination that wrongly assumes earliest first.
            return httpx.Response(
                200,
                json=sorted(records, key=lambda r: r["time"], reverse=True)[: int(params["limit"])],
            )
        if path in {"/fapi/v1/income", "/dapi/v1/income"}:
            return httpx.Response(200, json=[income(), income(kind="REALIZED_PNL")])
        raise AssertionError("Unexpected Binance endpoint: " + path)
