from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

from folio.models import Record, Snapshot
from folio.store import Store


def test_actual_daemon_and_cli_follow_detach_and_cached_read(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = tmp_path / "folio.toml"
    config.write_text(f"""[server]
port = {port}
[storage]
path = {json.dumps(str(tmp_path / "state"))}
[connections.wolt]
provider = "wolt"
credential_adapter = true
""")
    executable = Path(sys.executable).parent
    command = [str(executable / "folio"), "--config", str(config)]
    with (tmp_path / "daemon.log").open("w") as log:
        daemon = subprocess.Popen(
            [str(executable / "foliod"), "--config", str(config)], stdout=log, stderr=log
        )
        follow = None
        try:
            with httpx.Client(trust_env=False) as client:
                for _ in range(100):
                    try:
                        assert client.get(f"http://127.0.0.1:{port}/healthz").is_success
                        break
                    except httpx.TransportError:
                        time.sleep(0.1)
                else:
                    raise AssertionError("daemon did not start")
            result = subprocess.run([*command, "connections"], check=True, capture_output=True)
            assert json.loads(result.stdout)[0]["id"] == "wolt"
            result = subprocess.run(
                [*command, "sync", "wolt", "--detach"], check=True, capture_output=True
            )
            job = json.loads(result.stdout)
            assert job["state"] == "queued"
            follow = subprocess.Popen(
                [*command, "job", job["id"], "--follow"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            # Fixture worker completes the same durable job the real CLI is following.
            store = Store(tmp_path / "state")
            store.claim("wolt")
            store.finish(
                job,
                Snapshot(
                    records=[
                        Record(id="fixture", resource="orders", data={"merchant": "Test café"})
                    ],
                    resources=["orders"],
                    complete=True,
                    coverage="fixture",
                ),
                300,
            )
            output, error = follow.communicate(timeout=10)
            assert follow.returncode == 0, error.decode()
            assert json.loads(output)["state"] == "completed"
            result = subprocess.run(
                [*command, "read", "wolt", "orders", "--since", "2026-10-01"],
                check=True,
                capture_output=True,
            )
            payload = json.loads(result.stdout)
            assert payload["items"][0]["merchant"] == "Test café"
            assert payload["undated_included"]
            assert payload["items"][0]["occurred_at"] is None
        finally:
            if follow is not None and follow.poll() is None:
                follow.terminate()
                follow.wait(timeout=5)
            daemon.terminate()
            daemon.wait(timeout=15)
