from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import time

import pytest

from folio.broker import ServiceBroker, identity_path, provision_identity, read_identity
from folio.config import Settings
from folio.configure import recipient_config
from folio.models import LoginRequired

ZONDD = os.environ.get("FOLIO_TEST_ZONDD")
FIELDS = [{"id": "password", "label": "Password", "kind": "secret"}]


def settings_for(path):
    return Settings.from_data(
        {
            "storage": {"path": str(path / "state")},
            "zond": {"socket": str(path / "run/credential.sock")},
            "connections": {"bog": {"provider": "bog", "credential_adapter": True}},
        },
        path / "folio.toml",
    )


@pytest.mark.parametrize("unsafe", ["mode", "symlink", "parent", "syntax"])
def test_connection_identity_refuses_unsafe_paths_and_values(tmp_path, unsafe):
    settings = settings_for(tmp_path)
    connection = settings.connection("bog")
    identity = provision_identity(settings, connection)
    path = identity_path(settings, connection)
    assert provision_identity(settings, connection) == identity
    if unsafe == "mode":
        path.chmod(0o644)
    elif unsafe == "symlink":
        path.rename(path.with_suffix(".original"))
        path.symlink_to(path.with_suffix(".original"))
    elif unsafe == "parent":
        path.parent.chmod(0o777)
    else:
        path.write_text("not a valid identity")
    with pytest.raises((ValueError, OSError)):
        read_identity(path)


@pytest.fixture
def real_broker(tmp_path):
    if not ZONDD:
        pytest.skip("set FOLIO_TEST_ZONDD for public credential API E2E")
    settings = settings_for(tmp_path)
    config = tmp_path / "zond.toml"
    config.write_text(
        recipient_config(
            f"runtime_dir = {json.dumps(str(tmp_path / 'run'))}\n",
            tmp_path / "folio.toml",
            {},
            settings,
        )
    )
    config.chmod(0o600)
    log = (tmp_path / "zond.log").open("w+")
    processes = []

    def start():
        process = subprocess.Popen(
            [ZONDD, "serve", "--config", str(config)], stdout=log, stderr=log
        )
        processes.append(process)
        for _ in range(200):
            if (tmp_path / "run/credential.sock").exists():
                return process
            time.sleep(0.025)
        raise AssertionError("Zond credential socket did not become ready")

    def request(body):
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(5)
            client.connect(str(tmp_path / "run/admin.sock"))
            client.sendall((json.dumps({"version": 1, "id": 1, "body": body}) + "\n").encode())
            return json.loads(client.makefile("rb").readline())["body"]

    process = start()
    try:
        yield settings, process, start, request, log
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
        log.close()


def test_public_api_withdraw_discards_inflight_answers_and_allows_next_step(real_broker):
    settings, _, _, request, log = real_broker

    async def run():
        broker = ServiceBroker(settings, settings.connection("bog"))
        try:
            for delivered in (False, True):
                ident = await broker.challenge("bog", FIELDS, "https://ibank.bog.ge")
                waiting = asyncio.create_task(broker.receive())
                await asyncio.sleep(0)
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)
                if delivered:
                    request(
                        {
                            "method": "answer_interaction",
                            "challenge_id": ident,
                            "values": {"password": "race-test-secret"},
                        }
                    )
                await broker.withdraw(ident)
                pending = request({"method": "pending"})
                assert not pending["interactions"]
                assert pending["recipients"][0]["state"] == "idle"
            audit = json.dumps(request({"method": "actions", "limit": 100}))
            assert "cancelled" in audit and "race-test-secret" not in audit
        finally:
            broker.close()

    asyncio.run(run())
    log.flush()
    log.seek(0)
    assert "race-test-secret" not in log.read()


def test_public_api_disconnect_fails_step_and_reconnect_has_fresh_form(real_broker):
    settings, process, start, request, _ = real_broker

    async def run():
        broker = ServiceBroker(settings, settings.connection("bog"))
        try:
            ident = await broker.challenge("bog", FIELDS, "https://ibank.bog.ge")
            waiting = asyncio.create_task(broker.receive())
            process.terminate()
            process.wait(timeout=10)
            with pytest.raises(LoginRequired):
                await asyncio.wait_for(waiting, 5)
            start()
            new = await broker.challenge("bog", FIELDS, "https://ibank.bog.ge")
            assert new != ident
            assert len(request({"method": "pending"})["interactions"]) == 1
            request({"method": "answer_interaction", "challenge_id": new, "values": None})
            answer = await broker.receive()
            assert answer["challenge_id"] == new and answer["values"] is None
        finally:
            broker.close()

    asyncio.run(run())


def test_public_api_reports_scope_and_identity_errors_without_opening_forms(real_broker):
    settings, _, _, request, _ = real_broker

    async def run():
        broker = ServiceBroker(settings, settings.connection("bog"))
        try:
            with pytest.raises(LoginRequired, match="origin_not_allowed"):
                await broker.challenge("bog", FIELDS, "https://untrusted.example")
            with pytest.raises(LoginRequired, match="field_not_allowed"):
                await broker.challenge(
                    "bog",
                    [{"id": "api_key", "label": "Key", "kind": "secret"}],
                    "https://ibank.bog.ge",
                )
            assert not request({"method": "pending"})["interactions"]
            broker.close()
            identity_path(settings, settings.connection("bog")).write_text("0" * 64)
            with pytest.raises(LoginRequired, match="authentication_failed"):
                await broker.challenge("bog", FIELDS, "https://ibank.bog.ge")
            assert not request({"method": "pending"})["interactions"]
        finally:
            broker.close()

    asyncio.run(run())
