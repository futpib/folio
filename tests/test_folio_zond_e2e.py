from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ZONDD = os.environ.get("FOLIO_TEST_ZONDD")


@pytest.mark.skipif(
    not ZONDD or not shutil.which("chromium"),
    reason="set FOLIO_TEST_ZONDD and install Chromium for real zond E2E",
)
@pytest.mark.parametrize("present", [(), ("username",), ("password",), ("username", "password")])
def test_real_zond_chromium_decline_retry_password_and_otp(tmp_path, present):
    expected = {"username": "fixture-user", "password": "folio-test-secret"}
    if present:
        (tmp_path / ".env.local").write_text(
            "".join(f"BOG_{name.upper()}={expected[name]}\n" for name in present)
        )
    missing = {name: value for name, value in expected.items() if name not in present}
    fixture = Path(__file__).with_name("folio_zond_fixture.py")
    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
    folio_config = tmp_path / "folio.toml"
    folio_config.write_text(f"""[server]
port = {port}
[storage]
path = {json.dumps(str(tmp_path / "state"))}
[browser]
executable = {json.dumps(shutil.which("chromium"))}
[connections.bog]
provider = "bog"
credential_adapter = true
min_sync_seconds = 1
""")
    cli = [str(Path(sys.executable).parent / "folio"), "--config", str(folio_config)]
    config = tmp_path / "zond.toml"
    config.write_text(f"""runtime_dir = {json.dumps(str(tmp_path / "run"))}
[credential_adapters.folio]
label = "Configured Folio fixture"
program = {json.dumps(cli[0])}
args = {json.dumps([*cli[1:], "credential-relay", "bog"])}
cwd = {json.dumps(str(tmp_path))}
""")
    config.chmod(0o600)

    def request(body):
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(5)
            client.connect(str(tmp_path / "run/admin.sock"))
            client.sendall((json.dumps({"version": 1, "id": 1, "body": body}) + "\n").encode())
            return json.loads(client.makefile("rb").readline())["body"]

    def wait(predicate):
        for _ in range(300):
            if value := predicate():
                return value
            time.sleep(0.1)
        raise AssertionError("Folio/zond E2E did not reach expected state")

    def call(*args):
        result = subprocess.run([*cli, *args], capture_output=True, timeout=10)
        assert result.returncode in {0, 1} and result.stdout, result.stderr.decode()
        return json.loads(result.stdout)

    def healthy():
        try:
            with httpx.Client(trust_env=False) as client:
                return client.get(f"http://127.0.0.1:{port}/healthz").is_success
        except httpx.TransportError:
            return False

    with (tmp_path / "daemon.log").open("w+") as log:
        daemon = subprocess.Popen([ZONDD, "serve", "--config", str(config)], stdout=log, stderr=log)
        service = subprocess.Popen(
            [sys.executable, str(fixture), "--config", str(folio_config)],
            cwd=tmp_path,
            stdout=log,
            stderr=log,
        )
        try:
            wait(lambda: (tmp_path / "run/admin.sock").exists())
            wait(healthy)
            job = call("sync", "bog", "--detach")

            def challenge():
                return wait(lambda: request({"method": "pending"})["interactions"])[0]

            if missing:
                first = challenge()
                assert first["recipient"]["label"] == "Configured Folio fixture"
                assert [f["id"] for f in first["fields"]] == list(missing)
                assert "folio-test-secret" not in json.dumps(first)
                request(
                    {
                        "method": "answer_interaction",
                        "challenge_id": first["challenge_id"],
                        "values": None,
                    }
                )
                wait(lambda: call("job", job["id"])["state"] == "failed")
                assert not request({"method": "pending"})["interactions"]
                job = call("sync", "bog", "--detach")
                second = challenge()
                assert first["challenge_id"] != second["challenge_id"]
                assert [f["id"] for f in second["fields"]] == list(missing)
                request(
                    {
                        "method": "answer_interaction",
                        "challenge_id": second["challenge_id"],
                        "values": missing,
                    }
                )
            third = challenge()
            assert [f["id"] for f in third["fields"]] == ["otp"]
            request(
                {
                    "method": "answer_interaction",
                    "challenge_id": third["challenge_id"],
                    "values": {"otp": "527941"},
                }
            )
            wait(lambda: call("job", job["id"])["state"] == "completed")
            assert not request({"method": "pending"})["interactions"]
            result = subprocess.run(
                [*cli, "logs", "bog", "--job", job["id"]], capture_output=True, check=True
            )
            assert b"request_finished" in result.stdout
            assert b"/<redacted-path>" in result.stdout and b"status=200" in result.stdout
            assert b"folio-test-secret" not in result.stdout and b"527941" not in result.stdout
        finally:
            for process in (service, daemon):
                process.terminate()
                process.wait(timeout=15)
        log.seek(0)
        assert "folio-test-secret" not in log.read()
