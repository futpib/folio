import asyncio
import json
import sqlite3
from dataclasses import replace

import httpx
import pytest
from binance_fixture import KEY, SECRET, Api
from test_binance import connection, connector

from folio.models import ConnectorError
from folio.observability import recording
from folio.store import Store


@pytest.mark.parametrize("failure", [None, "auth", "network", "cancelled"])
def test_request_logs_have_safe_metadata_for_success_failure_and_cancellation(
    tmp_path, capsys, failure
):
    api = Api()
    configured = replace(connection(), min_sync_seconds=1)
    worker = connector(api, configured)
    if failure == "auth":
        api.failure = lambda _: httpx.Response(401, json={"code": -2015, "msg": KEY + SECRET})
    elif failure == "network":

        def broken(request):
            raise httpx.ConnectError(KEY + SECRET, request=request)

        worker.transport = httpx.MockTransport(broken)
    elif failure == "cancelled":

        def cancelled(request):
            raise asyncio.CancelledError(KEY + SECRET)

        worker.transport = httpx.MockTransport(cancelled)
    store = Store(tmp_path)
    with recording(store, configured, "job-fixture"):
        if failure:
            with pytest.raises((ConnectorError, asyncio.CancelledError)):
                asyncio.run(worker.sync(None))
        else:
            asyncio.run(worker.sync(None))
    logs = store.logs(configured.id)["items"]
    starts = [row for row in logs if row["event"] == "request_started"]
    ends = [row for row in logs if row["event"] == "request_finished"]
    assert starts and len(starts) == len(ends)
    assert all(row["job"] == "job-fixture" for row in logs)
    assert all("duration_ms" in row["fields"] for row in ends)
    output = capsys.readouterr().err
    material = json.dumps(logs) + output
    assert KEY not in material and SECRET not in material
    assert "timestamp=" not in material and "signature=" not in material
    if failure == "auth":
        assert ends[-1]["fields"]["status"] == 401 and ends[-1]["fields"]["code"] == -2015
    elif failure:
        assert ends[-1]["fields"]["error_code"] == failure


def test_log_retention_and_cursor_filters_do_not_remove_account_data(tmp_path):
    configured = connection()
    store = Store(tmp_path)
    store.register(configured)
    with store.db() as db:
        db.executemany(
            "INSERT INTO logs(connection,provider,job,created,event,fields) VALUES(?,?,?,?,?,?)",
            [
                (configured.id, "binance", "old-job", i, "request_started", "{}")
                for i in range(10005)
            ],
        )
    last = store.append_log(configured, "new-job", "job_started", {})
    with store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM logs").fetchone()[0] == 10000
        assert db.execute("SELECT COUNT(*) FROM connections").fetchone()[0] == 1
    assert store.logs(configured.id, job="new-job")["items"] == [last]
    assert not store.logs(configured.id, after=last["id"])["items"]
    assert not store.logs("different")["items"]


def test_log_storage_failure_does_not_fail_a_successful_provider_request(
    tmp_path, monkeypatch, capsys
):
    configured = connection()
    store = Store(tmp_path)

    def unavailable(*args):
        raise sqlite3.OperationalError(KEY + SECRET)

    monkeypatch.setattr(store, "append_log", unavailable)
    with recording(store, configured):
        result = asyncio.run(connector(Api(), configured).sync(None))
    assert result.complete
    output = capsys.readouterr().err
    assert KEY not in output and SECRET not in output
    assert '"log_persistence":"failed"' in output and '"status":200' in output
