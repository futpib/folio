from __future__ import annotations

import httpx

from ibkr_tws_http.app import create_app
from ibkr_tws_http.config import Settings as IbkrSettings

from .calls import CallRequest, response_result, target
from .config import Connection
from .models import ConnectorError, Record, Snapshot


class IbkrConnector:
    def __init__(self, connection: Connection, app=None):
        self.app = app or create_app(IbkrSettings.load(connection.ibkr_config))

    async def call(self, request: CallRequest):
        if request.public:
            raise ValueError("--public applies to Binance calls")
        url, params = target(request, {"http://ibkr"}, "http://ibkr")
        async with (
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app),
                base_url="http://ibkr",
                trust_env=False,
                follow_redirects=False,
            ) as client,
            client.stream(
                request.method,
                url,
                params=params,
                content=request.body,
                headers={"Content-Type": "application/json"} if request.body is not None else {},
            ) as response,
        ):
            return await response_result(response)

    async def sync(self, since: str | None):
        records = []
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://ibkr"
        ) as client:
            for resource, route in (
                ("accounts", "accounts"),
                ("balances", "account-summary"),
                ("positions", "positions"),
            ):
                response = await client.get(f"/v1/{route}")
                if response.status_code != 200:
                    raise ConnectorError(
                        "IBKR read failed. Check Gateway connectivity and the IBKR exposure policy."
                    )
                for item in response.json():
                    ident = item["account"]
                    if resource == "balances":
                        ident += f":{item['tag']}:{item['currency']}"
                    elif resource == "positions":
                        ident += f":{item['contract']['con_id']}"
                    records.append(
                        Record(
                            id=ident,
                            resource=resource,
                            data={
                                "provider": "ibkr",
                                **item,
                            },
                        )
                    )
        return Snapshot(
            records=records,
            resources=["accounts", "balances", "positions"],
            complete=True,
            coverage="current values allowed by the IBKR exposure policy",
        )
