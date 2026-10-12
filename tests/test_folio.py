from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from folio.config import Connection, Settings
from folio.ibkr import IbkrConnector
from folio.models import Record, SchemaChanged, Snapshot
from folio.providers import BOG_ACCOUNTS, parse, resource_for
from folio.service import create_app
from folio.store import Store
from ibkr_tws_http.app import create_app as ibkr_app
from ibkr_tws_http.config import ServerConfig
from ibkr_tws_http.config import Settings as IbkrSettings
from ibkr_tws_http.mock_tws import create_mock_gateway


def order(ident="order-1"):
    return {
        "purchase_id": ident,
        "timestamp": "2026-10-12T01:00:00Z",
        "total": "GEL 25.10",
        "status": {"value": "Delivered"},
        "venue": {"name": "Fixture café"},
        "items": [{"id": "item-1", "name": "Lunch", "count": 1}],
    }


def accounts():
    return {
        "result": {
            "accounts": {
                "accounts": [
                    {
                        "acctKey": 1,
                        "mainAcctKey": 1,
                        "acctName": "Current",
                        "printAcctNo": "GE-example",
                    }
                ]
            },
            "details": [
                {
                    "mainAcctKey": 1,
                    "subAccounts": [
                        {"acctKey": 1, "ccy": "GEL", "realAmount": "123.4567890123456789"},
                        {"acctKey": 2, "ccy": "USD", "realAmount": "50.10"},
                    ],
                }
            ],
        }
    }


def transaction(index):
    return {
        "docKey": index,
        "entryId": 100 + index,
        "operationDate": "2026-10-12T01:00:00Z",
        "accountKey": 1,
        "amount": "-25.10",
        "ccy": "GEL",
        "nomination": "Lunch",
        "sort": [index, 100 + index],
    }


def test_normalizers_preserve_amounts_currency_and_source_identity():
    result = parse("bog", "accounts", accounts())
    assert result[1].data["balance"] == {"amount": "123.4567890123456789", "currency": "GEL"}
    assert result[2].data["balance"]["currency"] == "USD"
    result = parse("bog", "transactions", {"data": [transaction(1)]})
    assert result[0].id == "1:101"
    assert result[0].data["amount"]["amount"] == "-25.10"
    payload = order()
    payload["access_token"] = "secret"
    result = parse("wolt", "orders", {"orders": [payload]})
    assert result[0].data["display_total"] == "GEL 25.10"
    assert result[0].data["amount"] is None
    assert "secret" not in result[0].model_dump_json()
    with pytest.raises(SchemaChanged):
        parse("wolt", "orders", {"orders": [{}]})
    with pytest.raises(SchemaChanged):
        parse("bog", "transactions", {"data": [dict(transaction(1), amount="NaN")]})


def test_only_read_endpoints_are_recognized():
    assert (
        resource_for("bog", f"https://ibank.bog.ge/rest/rb?serviceId={BOG_ACCOUNTS}") == "accounts"
    )
    assert (
        resource_for("bog", "https://ibank.bog.ge/rest/rb?serviceId=ACCOUNTS_CLOSE_ACCOUNT") is None
    )
    assert resource_for("wolt", "https://evil.test/order-xp/web/v1/pages/orders") is None
    assert resource_for("wolt", "https://wolt.com/order-xp/web/v1/pages/orders", "POST") is None


def test_durable_sync_deduplicates_keeps_old_records_and_recovers(tmp_path):
    connection = Connection("wolt", "wolt", min_sync_seconds=1)
    store = Store(tmp_path)
    store.register(connection)
    first = store.queue("wolt", None)
    assert store.queue("wolt", None)["id"] == first["id"]
    job = store.claim("wolt")
    assert job and store.claim("wolt") is None
    snap = Snapshot(
        records=parse("wolt", "orders", {"orders": [order()]}),
        resources=["orders"],
        complete=True,
        coverage="fixture",
    )
    store.finish(job, snap, 300)
    before = store.read("wolt", "orders", None, 100, 0)
    next_job = store.queue("wolt", None)
    assert store.claim("wolt") is None  # cooldown survives restart
    restarted = Store(tmp_path)
    assert restarted.job(first["id"])["state"] == "completed"
    restarted.fail(next_job, "schema_changed", "Format changed.", 300)
    assert restarted.read("wolt", "orders", None, 100, 0) == before
    with restarted.db() as db:
        db.execute("UPDATE connections SET next_sync=0")
    last = restarted.queue("wolt", None)
    restarted.claim("wolt")
    restarted.recover("wolt")
    assert restarted.job(last["id"])["error_code"] == "interrupted"
    assert tmp_path.stat().st_mode & 0o777 == 0o700
    assert restarted.path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):
        restarted.register(Connection("wolt", "bog"))


class GateConnector:
    def __init__(self):
        self.release = False
        self.calls = 0

    async def sync(self, since):
        self.calls += 1
        while not self.release:
            await asyncio.sleep(0.01)
        return Snapshot(
            records=[Record(id="one", resource="orders", data={"name": "order"})],
            resources=["orders"],
            complete=True,
            coverage="test",
        )


def wait_job(client, ident):
    for _ in range(500):
        result = client.get(f"/v1/jobs/{ident}").json()
        if result["state"] not in {"queued", "running"}:
            return result
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_api_read_sync_freshness_and_cross_origin_protection(tmp_path):
    settings = Settings(tmp_path, (Connection("wolt", "wolt", min_sync_seconds=1),))
    connector = GateConnector()
    with TestClient(create_app(settings, connectors={"wolt": connector})) as client:
        assert client.get("/v1/connections").json()[0]["resources"] == ["orders"]
        assert client.get("/v1/connections/wolt/records/orders").json()["snapshot"] is None
        first = client.post("/v1/connections/wolt/sync", json={}).json()
        assert client.post("/v1/connections/wolt/sync", json={}).json()["id"] == first["id"]
        connector.release = True
        assert wait_job(client, first["id"])["state"] == "completed"
        result = client.get("/v1/connections/wolt/records/orders").json()
        assert result["items"][0]["name"] == "order"
        assert result["snapshot"]["complete"]
        assert (
            client.get("/v1/connections/wolt/records/orders", params={"limit": 0}).status_code
            == 422
        )
        assert client.post("/v1/connections/wolt/sync", json={"url": "evil"}).status_code == 422
        assert client.get("/v1/connections/wolt/records/cookies").status_code == 404
        assert client.get("/v1/connections", headers={"Host": "attacker.test"}).status_code == 400
        assert (
            client.post(
                "/v1/connections/wolt/sync", json={}, headers={"Origin": "https://attacker.test"}
            ).status_code
            == 403
        )
    assert connector.calls == 1


def test_ibkr_compatibility_uses_existing_exposure_policy(tmp_path):
    old = IbkrSettings(server=ServerConfig())
    old = replace(old, exposure=replace(old.exposure, allowed_accounts=frozenset({"DU424242"})))
    connection = Connection("ibkr", "ibkr")
    underlying = ibkr_app(old, create_mock_gateway(old.tws))
    connector = IbkrConnector(connection, app=underlying)
    app = create_app(Settings(tmp_path, (connection,)), connectors={"ibkr": connector})
    with TestClient(app) as client:
        assert client.get("/v1/status").status_code == 200
        assert client.get("/v1/connections/ibkr/ibkr/v1/status").status_code == 200
        job = client.post("/v1/connections/ibkr/sync", json={}).json()
        assert wait_job(client, job["id"])["state"] == "completed"
        data = client.get("/v1/connections/ibkr/records/positions").json()
        assert "DU424242" not in json.dumps(data)
        assert len(data["items"]) == 2
        assert client.post("/v1/order", json={}).status_code == 404
        logs = client.get("/v1/connections/ibkr/logs", params={"job": job["id"]}).json()["items"]
        requests = [row for row in logs if row["event"] == "request_finished"]
        assert {row["fields"]["endpoint"] for row in requests} == {
            "ibkr://gateway/v1/accounts",
            "ibkr://gateway/v1/account-summary",
            "ibkr://gateway/v1/positions",
        }
        assert all(row["fields"]["status"] == 200 for row in requests)
        assert "DU424242" not in json.dumps(logs)


def test_config_rejects_unsafe_or_ambiguous_connections(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[connections.personal]\nprovider="wolt"\n')
    assert Settings.load(path).connections[0].resources == ("orders",)
    for content in (
        '[connections."../escape"]\nprovider="wolt"',
        '[connections.personal]\nprovider="wolt"\nurl="https://attacker.test"',
        '[server]\nhost="0.0.0.0"\n[connections.personal]\nprovider="wolt"',
        '[connections.personal]\nprovider="wolt"\nmin_sync_seconds=0',
    ):
        path.write_text(content)
        with pytest.raises(ValueError):
            Settings.load(path)


def test_ibkr_config_change_invalidates_previous_exposure_snapshot(tmp_path):
    config = tmp_path / "ibkr.toml"
    config.write_text("[exposure]\nexpose_account_ids=true\n")
    connection = Connection("ibkr", "ibkr", ibkr_config=config)
    store = Store(tmp_path / "state")
    store.register(connection)
    job = store.queue("ibkr", None)
    store.claim("ibkr")
    store.finish(
        job,
        Snapshot(
            records=[
                Record(
                    id="private-account", resource="accounts", data={"account": "private-account"}
                )
            ],
            resources=["accounts"],
            complete=True,
            coverage="test",
        ),
        300,
    )
    store.register(connection)
    assert store.read("ibkr", "accounts", None, 100, 0)["items"]
    config.write_text("[exposure]\nexpose_account_ids=false\n")
    store.register(connection)
    result = store.read("ibkr", "accounts", None, 100, 0)
    assert result["items"] == [] and result["snapshot"] is None
