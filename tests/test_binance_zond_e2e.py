from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from binance_fixture import KEY, SECRET
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

from folio.broker import identity_path
from folio.config import Settings
from folio.configure import recipient_config
from folio.store import Store

ZONDD = os.environ.get("FOLIO_TEST_ZONDD")


@pytest.mark.skipif(not ZONDD, reason="set FOLIO_TEST_ZONDD for real public credential API E2E")
@pytest.mark.parametrize("key_type", ["hmac", "ed25519", "rsa"])
def test_real_cli_http_worker_and_zond_decline_retry_authentication_and_restart(tmp_path, key_type):
    signing_field = "api_secret" if key_type == "hmac" else "private_key"
    signing_value = SECRET
    encrypted_signing_value = SECRET
    if key_type != "hmac":
        private = (
            ed25519.Ed25519PrivateKey.generate()
            if key_type == "ed25519"
            else rsa.generate_private_key(public_exponent=65537, key_size=2048)
        )
        signing_value = (
            private.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            .decode()
            .replace("\n", "\\n")
        )
        encrypted_signing_value = (
            private.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.BestAvailableEncryption(b"FixtureKeyPassphrase"),
            )
            .decode()
            .replace("\n", "\\n")
        )
        (tmp_path / "fixture-public.pem").write_bytes(
            private.public_key().public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
        )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = tmp_path / "folio.toml"
    config.write_text(f"""[server]
port = {port}
[storage]
path = {json.dumps(str(tmp_path / "state"))}
[zond]
socket = {json.dumps(str(tmp_path / "run/credential.sock"))}
[connections.binance]
provider = "binance"
credential_adapter = true
min_sync_seconds = 1
[connections.binance.binance]
key_type = "{key_type}"
markets = ["spot", "usdm", "coinm"]
spot_symbols = ["BTCUSDT"]
usdm_symbols = ["BTCUSDT"]
coinm_symbols = ["BTCUSD_PERP"]
history_days = 1
""")
    zond_config = tmp_path / "zond.toml"
    settings = Settings.load(config)
    zond_config.write_text(
        recipient_config(
            f"runtime_dir = {json.dumps(str(tmp_path / 'run'))}\n", config, {}, settings
        ).replace("Folio binance (binance)", "Configured Binance API fixture")
    )
    zond_config.chmod(0o600)
    binaries = Path(sys.executable).parent
    cli = [str(binaries / "folio"), "--config", str(config)]

    def call(*args):
        result = subprocess.run([*cli, *args], capture_output=True, timeout=10, check=False)
        assert result.returncode in {0, 1} and result.stdout, result.stderr.decode()
        return json.loads(result.stdout)

    def request(body):
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(5)
            client.connect(str(tmp_path / "run/admin.sock"))
            client.sendall((json.dumps({"version": 1, "id": 1, "body": body}) + "\n").encode())
            return json.loads(client.makefile("rb").readline())["body"]

    def wait(predicate):
        for _ in range(200):
            if value := predicate():
                return value
            time.sleep(0.05)
        raise AssertionError("Binance API adapter did not reach expected state")

    with (tmp_path / "daemon.log").open("w+") as log:
        folio = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).with_name("binance_zond_fixture.py")),
                "--daemon",
                "--config",
                str(config),
            ],
            stdout=log,
            stderr=log,
            cwd=tmp_path,
        )
        zond = subprocess.Popen(
            [ZONDD, "serve", "--config", str(zond_config)], stdout=log, stderr=log
        )
        smoke = None
        try:
            wait(lambda: (tmp_path / "run/admin.sock").exists())

            def healthy():
                try:
                    with httpx.Client(trust_env=False) as client:
                        return client.get(f"http://127.0.0.1:{port}/healthz").is_success
                except httpx.HTTPError:
                    return False

            wait(healthy)
            call("call", "binance", "/api/v3/time")
            smoke = subprocess.Popen(
                [
                    *cli,
                    "read",
                    "binance",
                    "trades",
                    "--live",
                    "--market",
                    "usdm",
                    "--symbol",
                    "BTCUSDT",
                    "--account-mode",
                    "classic",
                    "--verbose",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            live_form = wait(lambda: request({"method": "pending"})["interactions"])[0]
            request(
                {
                    "method": "answer_interaction",
                    "challenge_id": live_form["challenge_id"],
                    "values": None,
                }
            )
            live_out, live_err = smoke.communicate(timeout=10)
            assert smoke.returncode == 1 and json.loads(live_out)["error_code"] == "login_required"
            assert b"live_read_failed" in live_err
            assert not request({"method": "pending"})["interactions"]
            smoke = subprocess.Popen(
                [*cli, "call", "binance", "/api/v3/account", "--verbose"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            smoke_form = wait(lambda: request({"method": "pending"})["interactions"])[0]
            assert [f["id"] for f in smoke_form["fields"]] == ["api_key", signing_field]
            request(
                {
                    "method": "answer_interaction",
                    "challenge_id": smoke_form["challenge_id"],
                    "values": None,
                }
            )
            smoke_out, smoke_err = smoke.communicate(timeout=10)
            assert smoke.returncode == 1 and not smoke_out
            assert b"declined or expired" in smoke_err
            assert not request({"method": "pending"})["interactions"]
            smoke = subprocess.Popen(
                [
                    *cli,
                    "read",
                    "binance",
                    "trades",
                    "--live",
                    "--market",
                    "usdm",
                    "--symbol",
                    "BTCUSDT",
                    "--account-mode",
                    "classic",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            wait(lambda: request({"method": "pending"})["interactions"])
            smoke.terminate()
            smoke.communicate(timeout=10)
            wait(lambda: not request({"method": "pending"})["interactions"])
            if key_type == "hmac":
                smoke = subprocess.Popen(
                    [*cli, "call", "binance", "/api/v3/account"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                wait(lambda: request({"method": "pending"})["interactions"])
                folio.terminate()
                folio.wait(timeout=15)
                smoke.communicate(timeout=10)
                wait(lambda: not request({"method": "pending"})["interactions"])
                folio = subprocess.Popen(
                    [
                        sys.executable,
                        str(Path(__file__).with_name("binance_zond_fixture.py")),
                        "--daemon",
                        "--config",
                        str(config),
                    ],
                    stdout=log,
                    stderr=log,
                    cwd=tmp_path,
                )
                wait(healthy)
            key_file = identity_path(settings, settings.connection("binance"))
            assert key_file.stat().st_mode & 0o777 == 0o600
            assert key_file.parent.stat().st_mode & 0o777 == 0o700
            # A recipient name and matching UID alone do not authenticate a client.
            with socket.socket(socket.AF_UNIX) as intruder:
                intruder.settimeout(5)
                intruder.connect(str(tmp_path / "run/credential.sock"))
                intruder.sendall(
                    (
                        json.dumps(
                            {
                                "version": 1,
                                "id": 1,
                                "body": {
                                    "method": "authenticate",
                                    "recipient": "folio-binance",
                                    "key": "0" * 64,
                                },
                            }
                        )
                        + "\n"
                    ).encode()
                )
                assert (
                    json.loads(intruder.makefile("rb").readline())["body"]["code"]
                    == "authentication_failed"
                )
            job = call("sync", "binance", "--detach")
            first = wait(lambda: request({"method": "pending"})["interactions"])[0]
            assert first["recipient"]["label"] == "Configured Binance API fixture"
            assert [f["id"] for f in first["fields"]] == ["api_key", signing_field]
            request(
                {
                    "method": "answer_interaction",
                    "challenge_id": first["challenge_id"],
                    "values": None,
                }
            )
            wait(lambda: call("job", job["id"])["state"] == "failed")
            assert call("read", "binance", "balances")["snapshot"] is None
            assert not request({"method": "pending"})["interactions"]
            rejected = call("sync", "binance", "--detach")
            second = wait(lambda: request({"method": "pending"})["interactions"])[0]
            request(
                {
                    "method": "answer_interaction",
                    "challenge_id": second["challenge_id"],
                    "values": {"api_key": "WrongFixtureApiKey123", signing_field: signing_value},
                }
            )
            wait(lambda: call("job", rejected["id"])["state"] == "failed")
            failed = call("connections")[0]
            assert failed["error_code"] == "login_required"
            assert "spot account (/api/v3/account, HTTP 401, code -2015)" in failed["error"]
            assert KEY not in failed["error"] and SECRET not in failed["error"]
            assert call("read", "binance", "positions")["snapshot"] is None
            successful = call("sync", "binance", "--detach")
            third = wait(lambda: request({"method": "pending"})["interactions"])[0]
            assert len({c["challenge_id"] for c in (first, second, third)}) == 3
            denied = tmp_path / "denied-endpoints.json"
            denied.write_text(json.dumps(["/fapi/v3/positionRisk"]))
            request(
                {
                    "method": "answer_interaction",
                    "challenge_id": third["challenge_id"],
                    "values": {"api_key": KEY, signing_field: encrypted_signing_value},
                }
            )
            if key_type != "hmac":
                unlock = wait(lambda: request({"method": "pending"})["interactions"])[0]
                assert [f["id"] for f in unlock["fields"]] == ["private_key_passphrase"]
                assert unlock["challenge_id"] != third["challenge_id"]
                request(
                    {
                        "method": "answer_interaction",
                        "challenge_id": unlock["challenge_id"],
                        "values": {"private_key_passphrase": "FixtureKeyPassphrase"},
                    }
                )
            wait(lambda: call("job", successful["id"])["state"] == "completed")
            partial = call("job", successful["id"])
            assert partial["complete"] is False and len(partial["warnings"]) == 1
            assert len(call("read", "binance", "positions")["items"]) == 2
            assert not request({"method": "pending"})["interactions"]
            denied.unlink()
            recovered = call("sync", "binance", "--detach")
            wait(lambda: call("job", recovered["id"])["state"] == "completed")
            assert call("job", recovered["id"])["complete"] is True
            assert not request({"method": "pending"})["interactions"]
            positions = call("read", "binance", "positions")
            assert len(positions["items"]) == 4
            assert positions["snapshot"]["complete"]
            assert len(call("read", "binance", "trades")["items"]) == 3
            assert len(call("read", "binance", "income")["items"]) == 4
            cached = call("sync", "binance", "--detach")
            wait(lambda: call("job", cached["id"])["state"] == "completed")
            positions = call("read", "binance", "positions")
            assert not request({"method": "pending"})["interactions"]
            zond.terminate()
            zond.wait(timeout=10)
            independent = call("sync", "binance", "--detach")
            wait(lambda: call("job", independent["id"])["state"] == "completed")
            positions = call("read", "binance", "positions")
            # Zond owns authentication only; restarting it does not discard Folio's valid keys.
            for account_type, mode in (
                ("PM_2", "portfolio"),
                ("PM_1", "portfolio_pro"),
                ("PM_3", "portfolio_pro_span"),
                (None, "classic"),
            ):
                (tmp_path / "account-mode.json").write_text(json.dumps(account_type))
                changed = call("sync", "binance", "--detach")
                wait(lambda job=changed: call("job", job["id"])["state"] == "completed")
                assert call("job", changed["id"])["complete"]
                positions = call("read", "binance", "positions")
                assert len(positions["items"]) == 4
                assert {p["account_mode"] for p in positions["items"]} == {mode}
            zond = subprocess.Popen(
                [ZONDD, "serve", "--config", str(zond_config)], stdout=log, stderr=log
            )
            folio.terminate()
            folio.wait(timeout=15)
            folio = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).with_name("binance_zond_fixture.py")),
                    "--daemon",
                    "--config",
                    str(config),
                ],
                stdout=log,
                stderr=log,
                cwd=tmp_path,
            )
            wait(healthy)
            # A Folio restart drops its in-memory keys; missing local fields now need Zond again.
            restarted_job = call("sync", "binance", "--detach")

            def restarted_form():
                try:
                    return request({"method": "pending"})["interactions"]
                except (OSError, ValueError):
                    return None

            fourth = wait(restarted_form)[0]
            assert fourth["challenge_id"] != third["challenge_id"]
            assert call("read", "binance", "positions")["items"] == positions["items"]
            if key_type == "hmac":
                # Stopping the standalone service withdraws the still-pending form.
                folio.terminate()
                folio.wait(timeout=15)
                wait(lambda: not request({"method": "pending"})["interactions"])
                assert Store(tmp_path / "state").job(restarted_job["id"])["state"] == "failed"
                folio = subprocess.Popen(
                    [
                        sys.executable,
                        str(Path(__file__).with_name("binance_zond_fixture.py")),
                        "--daemon",
                        "--config",
                        str(config),
                    ],
                    stdout=log,
                    stderr=log,
                    cwd=tmp_path,
                )
                wait(healthy)
            else:
                request(
                    {
                        "method": "answer_interaction",
                        "challenge_id": fourth["challenge_id"],
                        "values": None,
                    }
                )
            wait(lambda: call("job", restarted_job["id"])["state"] == "failed")
            env_file = tmp_path / ".env.local"
            env_file.write_text(f"BINANCE_API_KEY={KEY}\n")
            partial = call("sync", "binance", "--detach")
            fifth = wait(restarted_form)[0]
            assert [f["id"] for f in fifth["fields"]] == [signing_field]
            assert KEY not in json.dumps(fifth)
            request(
                {
                    "method": "answer_interaction",
                    "challenge_id": fifth["challenge_id"],
                    "values": {signing_field: signing_value},
                }
            )
            wait(lambda: call("job", partial["id"])["state"] == "completed")
            zond.terminate()
            zond.wait(timeout=10)
            env_file.write_text(
                f"BINANCE_API_KEY={KEY}\nBINANCE_{signing_field.upper()}={signing_value}\n"
            )
            (tmp_path / "forbid-zond").touch()
            folio.terminate()
            folio.wait(timeout=15)
            folio = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).with_name("binance_zond_fixture.py")),
                    "--daemon",
                    "--config",
                    str(config),
                ],
                stdout=log,
                stderr=log,
                cwd=tmp_path,
            )
            wait(healthy)
            from_file = call("sync", "binance", "--detach")
            wait(lambda: call("job", from_file["id"])["state"] == "completed")
            detail = subprocess.run(
                [*cli, "job", from_file["id"], "--verbose"], capture_output=True, check=True
            )
            assert json.loads(detail.stdout)["state"] == "completed"
            assert b"request_started" in detail.stderr and b"request_finished" in detail.stderr
            assert KEY.encode() not in detail.stderr and SECRET.encode() not in detail.stderr
            history = subprocess.run(
                [*cli, "logs", "binance", "--job", from_file["id"]], capture_output=True, check=True
            )
            assert b"https://api.binance.com/api/v3/account" in history.stdout
            assert b"status=200" in history.stdout and b"duration_ms=" in history.stdout
            refreshed = call("read", "binance", "positions")["items"]
            assert [{k: v for k, v in row.items() if k != "observed_at"} for row in refreshed] == [
                {k: v for k, v in row.items() if k != "observed_at"} for row in positions["items"]
            ]
        finally:
            if smoke is not None and smoke.poll() is None:
                smoke.terminate()
                smoke.communicate(timeout=10)
            for process in (folio, zond):
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        log.seek(0)
        assert KEY not in log.read()
        for file in tmp_path.rglob("*"):
            if file.is_file() and file != tmp_path / ".env.local":
                content = file.read_bytes()
                assert KEY.encode() not in content and SECRET.encode() not in content, file
                assert signing_value.encode() not in content, file
                assert encrypted_signing_value.encode() not in content, file
                assert b"FixtureKeyPassphrase" not in content, file
