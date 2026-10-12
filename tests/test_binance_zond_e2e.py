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

ZONDD = os.environ.get("FOLIO_TEST_ZONDD")


@pytest.mark.skipif(not ZONDD, reason="set FOLIO_TEST_ZONDD for real API adapter E2E")
def test_real_cli_http_worker_and_zond_decline_retry_authentication_and_restart(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = tmp_path / "folio.toml"
    config.write_text(f"""[server]
port = {port}
[storage]
path = {json.dumps(str(tmp_path / "state"))}
[connections.binance]
provider = "binance"
credential_adapter = true
min_sync_seconds = 1
[connections.binance.binance]
markets = ["spot", "usdm", "coinm"]
spot_symbols = ["BTCUSDT"]
usdm_symbols = ["BTCUSDT"]
coinm_symbols = ["BTCUSD_PERP"]
history_days = 1
""")
    zond_config = tmp_path / "zond.toml"
    adapter_args = [
        str(Path(__file__).with_name("binance_zond_fixture.py")),
        "--config",
        str(config),
        "api-adapter",
        "binance",
    ]
    zond_config.write_text(f"""runtime_dir = {json.dumps(str(tmp_path / "run"))}
[credential_adapters.binance]
label = "Configured Binance API fixture"
program = {json.dumps(sys.executable)}
args = {json.dumps(adapter_args)}
cwd = {json.dumps(str(tmp_path))}
""")
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
            [str(binaries / "foliod"), "--config", str(config)], stdout=log, stderr=log
        )
        zond = subprocess.Popen(
            [ZONDD, "serve", "--config", str(zond_config)], stdout=log, stderr=log
        )
        try:
            wait(lambda: (tmp_path / "run/admin.sock").exists())
            with httpx.Client(trust_env=False) as client:

                def healthy():
                    try:
                        return client.get(f"http://127.0.0.1:{port}/healthz").is_success
                    except httpx.HTTPError:
                        return False

                wait(healthy)
            job = call("sync", "binance", "--detach")
            first = wait(lambda: request({"method": "pending"})["interactions"])[0]
            assert first["recipient"]["label"] == "Configured Binance API fixture"
            assert [f["id"] for f in first["fields"]] == ["api_key", "api_secret"]
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
            request({"method": "retry_adapter", "adapter": "binance"})
            second = wait(lambda: request({"method": "pending"})["interactions"])[0]
            request(
                {
                    "method": "answer_interaction",
                    "challenge_id": second["challenge_id"],
                    "values": {"api_key": "WrongFixtureApiKey123", "api_secret": SECRET},
                }
            )
            wait(lambda: request({"method": "pending"})["adapters"][0]["state"] == "unavailable")
            assert call("read", "binance", "positions")["snapshot"] is None
            request({"method": "retry_adapter", "adapter": "binance"})
            third = wait(lambda: request({"method": "pending"})["interactions"])[0]
            assert len({c["challenge_id"] for c in (first, second, third)}) == 3
            request(
                {
                    "method": "answer_interaction",
                    "challenge_id": third["challenge_id"],
                    "values": {"api_key": KEY, "api_secret": SECRET},
                }
            )
            wait(lambda: request({"method": "pending"})["adapters"][0]["state"] == "ready")
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
            zond = subprocess.Popen(
                [ZONDD, "serve", "--config", str(zond_config)], stdout=log, stderr=log
            )
            # The prior snapshot remains readable; the restarted adapter must ask for keys again.
            call("sync", "binance", "--detach")

            def restarted_form():
                try:
                    return request({"method": "pending"})["interactions"]
                except (OSError, ValueError):
                    return None

            fourth = wait(restarted_form)[0]
            assert fourth["challenge_id"] != third["challenge_id"]
            assert call("read", "binance", "positions")["items"] == positions["items"]
            request(
                {
                    "method": "answer_interaction",
                    "challenge_id": fourth["challenge_id"],
                    "values": None,
                }
            )
            wait(lambda: request({"method": "pending"})["adapters"][0]["state"] == "unavailable")
            env_file = tmp_path / ".env.local"
            env_file.write_text(f"BINANCE_API_KEY={KEY}\n")
            request({"method": "retry_adapter", "adapter": "binance"})
            fifth = wait(restarted_form)[0]
            assert [f["id"] for f in fifth["fields"]] == ["api_secret"]
            assert KEY not in json.dumps(fifth)
            request(
                {
                    "method": "answer_interaction",
                    "challenge_id": fifth["challenge_id"],
                    "values": {"api_secret": SECRET},
                }
            )
            wait(lambda: request({"method": "pending"})["adapters"][0]["state"] == "ready")
            zond.terminate()
            zond.wait(timeout=10)
            env_file.write_text(f"BINANCE_API_KEY={KEY}\nBINANCE_API_SECRET={SECRET}\n")
            zond = subprocess.Popen(
                [ZONDD, "serve", "--config", str(zond_config)], stdout=log, stderr=log
            )
            from_file = call("sync", "binance", "--detach")
            wait(lambda: call("job", from_file["id"])["state"] == "completed")
            assert not request({"method": "pending"})["interactions"]
            refreshed = call("read", "binance", "positions")["items"]
            assert [{k: v for k, v in row.items() if k != "observed_at"} for row in refreshed] == [
                {k: v for k, v in row.items() if k != "observed_at"} for row in positions["items"]
            ]
        finally:
            for process in (folio, zond):
                process.terminate()
                process.wait(timeout=15)
        log.seek(0)
        assert KEY not in log.read()
        for file in tmp_path.rglob("*"):
            if file.is_file() and file != tmp_path / ".env.local":
                content = file.read_bytes()
                assert KEY.encode() not in content and SECRET.encode() not in content, file
