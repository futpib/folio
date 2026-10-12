"""Opt-in setup smoke using isolated unit names on the real user systemd manager."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import tomllib
import uuid
from pathlib import Path

import httpx
import pytest

from folio import cli, configure
from folio.broker import identity_path
from folio.config import Settings

pytestmark = pytest.mark.skipif(
    os.environ.get("FOLIO_SETUP_E2E") != "1",
    reason="set FOLIO_SETUP_E2E=1 for real user-service setup",
)


def test_setup_registers_identity_restarts_standalone_service_and_rolls_back_failure(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("folio.credentials.DEFAULT_ENV_FILE", tmp_path / ".env.local")
    zondd = Path(os.environ["FOLIO_TEST_ZONDD"])
    zondctl = zondd.with_name("zondctl")
    token = uuid.uuid4().hex[:10]
    folio_unit = f"folio-setup-test-{token}.service"
    zond_unit = f"folio-zond-test-{token}.service"
    legacy_unit = f"folio-legacy-test-{token}.service"
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for name, executable in (("zondd", zondd), ("zondctl", zondctl)):
        (binaries / name).symlink_to(executable)
    monkeypatch.setenv("PATH", str(binaries) + os.pathsep + os.environ["PATH"])
    run_dir = tmp_path / "run"
    zond_config = config_home / "zondd/config.toml"
    zond_config.parent.mkdir()
    zond_config.write_text(f"runtime_dir = {json.dumps(str(run_dir))}\n")
    zond_config.chmod(0o600)
    unit = tmp_path / zond_unit
    unit.write_text(
        f"[Service]\nExecStart={zondd} serve --config {zond_config}\n"
        "[Install]\nWantedBy=default.target\n"
    )
    module = configure.installer()
    monkeypatch.setattr(module, "UNIT", folio_unit)
    monkeypatch.setattr(module, "ZOND_UNIT", zond_unit)
    monkeypatch.setattr(module, "LEGACY_UNIT", legacy_unit)
    real_installer = configure.installer
    monkeypatch.setattr(configure, "installer", lambda: module)
    config = config_home / "folio/config.toml"
    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
    arguments = [
        "folio",
        "--config",
        str(config),
        "setup",
        "binance",
        "--yes",
        "--no-linger",
        "--port",
        str(port),
        "--storage",
        str(tmp_path / "state"),
    ]

    def call(arguments):
        monkeypatch.setattr(sys, "argv", arguments)
        cli.main()

    def sessions():
        result = subprocess.run(
            [str(zondctl), "--runtime-dir", str(run_dir), "--json", "pending"],
            check=True,
            capture_output=True,
        )
        return json.loads(result.stdout)["recipients"]

    try:
        subprocess.run(["systemctl", "--user", "link", str(unit)], check=True, capture_output=True)
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            failed = [*arguments]
            failed[failed.index("--port") + 1] = str(occupied.getsockname()[1])
            with pytest.raises(SystemExit) as error:
                call(failed)
            assert error.value.code == 1
        assert not config.exists()
        assert not (config_home / "systemd/user" / folio_unit).exists()
        assert set(tomllib.loads(zond_config.read_text())) == {"runtime_dir"}
        fragment = subprocess.run(
            ["systemctl", "--user", "show", folio_unit, "--property=FragmentPath", "--value"],
            capture_output=True,
            check=False,
        )
        assert not fragment.stdout.strip()
        subprocess.run(["systemctl", "--user", "link", str(unit)], check=True, capture_output=True)
        call(arguments)
        with httpx.Client(trust_env=False) as client:
            result = client.get(f"http://127.0.0.1:{port}/v1/connections").json()
            assert result[0]["id"] == "binance" and result[0]["worker"] == "foliod"
        assert not sessions(), "Setup authenticates its test session then disconnects"
        settings = Settings.load(config)
        assert settings.zond_socket == run_dir / "credential.sock"
        key_file = identity_path(settings, settings.connection("binance"))
        identity = key_file.read_bytes()
        registered = zond_config.read_bytes()
        assert b"credential_adapters" not in registered and b"key_sha256" in registered
        assert identity.strip() not in registered
        assert key_file.stat().st_mode & 0o777 == 0o600
        assert key_file.parent.stat().st_mode & 0o777 == 0o700

        def folio_pid():
            return subprocess.run(
                ["systemctl", "--user", "show", folio_unit, "--property=MainPID", "--value"],
                check=True,
                capture_output=True,
            ).stdout

        initial_pid = folio_pid()
        with socket.socket(socket.AF_UNIX) as active_recipient:
            active_recipient.settimeout(5)
            active_recipient.connect(str(run_dir / "credential.sock"))
            active_recipient.sendall(
                (
                    json.dumps(
                        {
                            "version": 1,
                            "id": 1,
                            "body": {
                                "method": "authenticate",
                                "recipient": "folio-binance",
                                "key": identity.decode().strip(),
                            },
                        }
                    )
                    + "\n"
                ).encode()
            )
            assert json.loads(active_recipient.makefile("rb").readline())["body"]["type"] == "ready"
            # The setup probe can verify identity while another credential session is active.
            call([*arguments, "--history-days", "30"])
        assert folio_pid() != initial_pid
        assert key_file.read_bytes() == identity and zond_config.read_bytes() == registered
        assert not sessions(), "Idle Folio does not connect to Zond"
        assert Settings.load(config).connection("binance").binance.history_days == 30
        pid_before_zond_restart = folio_pid()
        subprocess.run(["systemctl", "--user", "restart", zond_unit], check=True)
        assert folio_pid() == pid_before_zond_restart
        unchanged_config = config.read_bytes()
        call([*arguments, "--history-days", "30"])
        assert config.read_bytes() == unchanged_config
        assert key_file.read_bytes() == identity and zond_config.read_bytes() == registered
        original = config.read_bytes()
        previous_unit = (config_home / "systemd/user" / folio_unit).read_bytes()
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            other_port = occupied.getsockname()[1]
            changed = [*arguments]
            changed[changed.index("--port") + 1] = str(other_port)
            with pytest.raises(SystemExit) as error:
                call(changed)
            assert error.value.code == 1
        assert config.read_bytes() == original
        assert (config_home / "systemd/user" / folio_unit).read_bytes() == previous_unit
        with httpx.Client(trust_env=False) as client:
            for _ in range(100):
                try:
                    assert client.get(f"http://127.0.0.1:{port}/v1/connections").is_success
                    break
                except httpx.HTTPError:
                    time.sleep(0.1)
            else:
                raise AssertionError("previous Folio service did not recover after failed setup")
    finally:
        monkeypatch.setattr(configure, "installer", real_installer)
        for name in (folio_unit, zond_unit):
            subprocess.run(
                ["systemctl", "--user", "disable", "--now", name], check=False, capture_output=True
            )
            subprocess.run(
                ["systemctl", "--user", "reset-failed", name], check=False, capture_output=True
            )
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False, capture_output=True)
