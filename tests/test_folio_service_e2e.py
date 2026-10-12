from __future__ import annotations

import json
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


@pytest.mark.parametrize("key_type", ["hmac", "ed25519", "rsa"])
def test_fresh_standalone_service_env_only_authentication_and_correction(tmp_path, key_type):
    signing_name, signing_value = "API_SECRET", SECRET
    if key_type != "hmac":
        private = (
            ed25519.Ed25519PrivateKey.generate()
            if key_type == "ed25519"
            else rsa.generate_private_key(public_exponent=65537, key_size=2048)
        )
        signing_name = "PRIVATE_KEY"
        signing_value = (
            private.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            .decode()
            .replace("\n", "\\n")
        )
        (tmp_path / "fixture-public.pem").write_bytes(
            private.public_key().public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
        )
    env = tmp_path / ".env.local"
    env.write_text(
        f"BINANCE_API_KEY=WrongFixtureApiKey123\nBINANCE_{signing_name}={signing_value}\n"
    )
    env.chmod(0o600)
    (tmp_path / "forbid-zond").touch()
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
key_type = "{key_type}"
markets = ["spot", "usdm", "coinm"]
spot_symbols = ["BTCUSDT"]
usdm_symbols = ["BTCUSDT"]
coinm_symbols = ["BTCUSD_PERP"]
""")
    cli = [str(Path(sys.executable).parent / "folio"), "--config", str(config)]
    with (tmp_path / "daemon.log").open("w+") as log:
        service = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).with_name("binance_zond_fixture.py")),
                "--daemon",
                "--config",
                str(config),
            ],
            cwd=tmp_path,
            stdout=log,
            stderr=log,
        )
        try:
            with httpx.Client(trust_env=False) as client:
                for _ in range(100):
                    try:
                        if client.get(f"http://127.0.0.1:{port}/healthz").is_success:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(0.05)
                else:
                    raise AssertionError("standalone Folio did not start")
            rejected = subprocess.run(
                [*cli, "sync", "binance", "--verbose"], capture_output=True, timeout=15
            )
            assert rejected.returncode == 1
            assert json.loads(rejected.stdout)["error_code"] == "login_required"
            assert b"status=401 code=-2015" in rejected.stderr
            env.write_text(f"BINANCE_API_KEY={KEY}\nBINANCE_{signing_name}={signing_value}\n")
            success = subprocess.run(
                [*cli, "sync", "binance", "--verbose"],
                capture_output=True,
                timeout=15,
                check=True,
            )
            assert json.loads(success.stdout)["state"] == "completed"
            assert b"request_finished" in success.stderr and b"status=200" in success.stderr
            assert b"credentials_requested" not in success.stderr + rejected.stderr

            def read(resource):
                response = subprocess.run(
                    [*cli, "read", "binance", resource],
                    capture_output=True,
                    timeout=15,
                    check=True,
                )
                return json.loads(response.stdout)

            before = {r: read(r) for r in ("balances", "positions", "trades")}
            denied = tmp_path / "denied-endpoints.json"
            denied.write_text(
                json.dumps(
                    [
                        "/fapi/v3/account",
                        "/fapi/v3/positionRisk",
                        "/fapi/v1/userTrades",
                        "/fapi/v1/income",
                    ]
                )
            )
            partial = subprocess.run(
                [*cli, "sync", "binance", "--verbose"],
                capture_output=True,
                timeout=15,
                check=True,
            )
            job = json.loads(partial.stdout)
            assert job["state"] == "completed" and job["complete"] is False
            assert len(job["warnings"]) == 4 and "/fapi/v3/account" in job["warnings"][0]
            assert (
                b"section_skipped" in partial.stderr and b"status=401 code=-2015" in partial.stderr
            )
            assert b"credentials_requested" not in partial.stderr
            for resource, previous in before.items():
                after = read(resource)
                assert [r for r in after["items"] if r["market"] == "usdm"] == [
                    r for r in previous["items"] if r["market"] == "usdm"
                ]
                old = next(s for s in previous["scopes"] if s["market"] == "usdm")
                stale = next(s for s in after["scopes"] if s["market"] == "usdm")
                assert stale["synced"] == old["synced"] and stale["status"] == "denied"
                fresh = next(s for s in after["scopes"] if s["market"] == "coinm")
                assert fresh["synced"] > stale["synced"] and fresh["status"] == "complete"
            denied.unlink()
            restored = subprocess.run(
                [*cli, "sync", "binance", "--verbose"],
                capture_output=True,
                timeout=15,
                check=True,
            )
            job = json.loads(restored.stdout)
            assert job["complete"] is True and job["warnings"] == []
            assert b"credentials_requested" not in restored.stderr
            refreshed = next(s for s in read("positions")["scopes"] if s["market"] == "usdm")
            assert refreshed["synced"] > before["positions"]["snapshot"]["synced"]
            assert refreshed["status"] == "complete"
        finally:
            service.terminate()
            service.wait(timeout=15)
        assert not (tmp_path / "state/daemon.pid").exists()
        log.seek(0)
        output = (
            log.read()
            + success.stderr.decode()
            + rejected.stderr.decode()
            + partial.stderr.decode()
        )
        assert KEY not in output and SECRET not in output and signing_value not in output
