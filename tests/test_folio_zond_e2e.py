from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

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
    if not missing:
        (tmp_path / "full-credentials").touch()
    fixture = Path(__file__).with_name("folio_zond_fixture.py")
    config = tmp_path / "zond.toml"
    config.write_text(f"""runtime_dir = {json.dumps(str(tmp_path / "run"))}
[credential_adapters.folio]
label = "Configured Folio fixture"
program = {json.dumps(sys.executable)}
args = {json.dumps([str(fixture), str(tmp_path)])}
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

    with (tmp_path / "daemon.log").open("w+") as log:
        daemon = subprocess.Popen([ZONDD, "serve", "--config", str(config)], stdout=log, stderr=log)
        try:
            wait(lambda: (tmp_path / "run/admin.sock").exists())

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
                wait(lambda: (tmp_path / "declined").exists())
                assert not request({"method": "pending"})["interactions"]
                request({"method": "retry_adapter", "adapter": "folio"})
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
            wait(lambda: (tmp_path / "authenticated").exists())
            wait(lambda: request({"method": "pending"})["adapters"][0]["state"] == "ready")
            assert not request({"method": "pending"})["interactions"]
        finally:
            daemon.terminate()
            daemon.wait(timeout=15)
        log.seek(0)
        assert "folio-test-secret" not in log.read()
