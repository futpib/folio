from __future__ import annotations

import importlib.util
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from ibkr_tws_http.config import Settings

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/setup-user-service.py"
spec = importlib.util.spec_from_file_location("service_setup", SCRIPT)
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


@pytest.mark.parametrize(
    "arguments,api_port,http_port",
    [
        ([], 7496, 8080),
        (["--gateway", "paper"], 4002, 8080),
        (["--gateway", "live"], 4001, 8080),
        (["--tws-port", "8080", "--http-port", "7496"], 8080, 7496),
    ],
)
def test_new_config_is_valid_and_uses_requested_ports(tmp_path, arguments, api_port, http_port):
    path = tmp_path / "http.toml"
    path.write_text(setup.http_config(path, setup.parser().parse_args(arguments)))
    settings = Settings.load(path)
    assert settings.tws.port == api_port
    assert settings.server.port == http_port
    assert settings.server.host == "127.0.0.1"


def test_rerun_preserves_exposure_policy_and_custom_ports(tmp_path):
    path = tmp_path / "http.toml"
    original = """# local choices
[server]
port = 9080
[tws]
port = 14002
[exposure]
allowed_accounts = ["DU424242"]
features = ["status", "positions"]
"""
    path.write_text(original)
    assert setup.http_config(path, setup.parser().parse_args(["--gateway", "paper"])) == original
    with pytest.raises(setup.SetupError, match="Edit the existing configuration"):
        setup.http_config(path, setup.parser().parse_args(["--tws-port", "4001"]))
    assert path.read_text() == original


def test_gateway_config_preserves_other_policy_and_is_idempotent(tmp_path):
    path = tmp_path / "zond.toml"
    original = """# Existing policy must survive setup.
[principals.default]
cwd = "/tmp"
allow_exec = false
[credential_adapters.other]
label = "Other application"
program = "/usr/bin/true"
cwd = "/tmp"
"""
    path.write_text(original)
    args = setup.parser().parse_args(
        ["--gateway", "paper", "--settings-dir", str(tmp_path / 'a "b')]
    )
    candidate = setup.gateway_config(path, args, 4002)
    assert candidate.startswith(original)
    data = tomllib.loads(candidate)
    assert data["principals"]["default"]["allow_exec"] is False
    assert data["credential_adapters"]["other"]["program"] == "/usr/bin/true"
    assert str(tmp_path / 'a "b') in data["credential_adapters"]["ibkr"]["args"]
    path.write_text(candidate)
    assert setup.gateway_config(path, args, 4002) == candidate
    with pytest.raises(setup.SetupError, match="different settings"):
        setup.gateway_config(path, args, 4001)
    assert path.read_text() == candidate


def test_failed_gateway_reload_restores_policy_and_retains_private_backup(tmp_path, monkeypatch):
    path = tmp_path / "zond.toml"
    original = '# user comment\n[principals.default]\ncwd = "/tmp"\n'
    path.write_text(original)
    candidate = setup.gateway_config(path, setup.parser().parse_args(["--gateway", "paper"]), 4002)

    def run(*command, **_kwargs):
        if command == ("zondctl", "reload"):
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(setup, "run", run)
    monkeypatch.setattr(
        setup.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0)
    )
    with pytest.raises(setup.SetupError, match="Restored"):
        setup.configure_gateway(path, candidate, original)
    assert path.read_text() == original
    (backup,) = tmp_path.glob("config.toml.backup-*")
    assert backup.read_text() == original
    assert backup.stat().st_mode & 0o777 == 0o600
    assert path.stat().st_mode & 0o777 == 0o600


def test_gateway_setup_does_not_overwrite_a_concurrent_policy_edit(tmp_path):
    path = tmp_path / "zond.toml"
    args = setup.parser().parse_args(["--gateway", "paper"])
    candidate = setup.gateway_config(path, args, 4002)
    changed = "# Changed in Android while setup was building.\n[limits]\nmax_running = 2\n"
    path.write_text(changed)
    with pytest.raises(setup.SetupError, match="changed during setup"):
        setup.configure_gateway(path, candidate, None)
    assert path.read_text() == changed


def test_manual_service_is_not_replaced(tmp_path, monkeypatch):
    unit = tmp_path / "systemd/user/ibkr-tws-http.service"
    unit.parent.mkdir(parents=True)
    original = "[Service]\nExecStart=/usr/bin/custom-bridge\n"
    unit.write_text(original)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(setup.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(setup, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(setup.os, "getuid", lambda: 1000)
    with pytest.raises(setup.SetupError, match="installed manually"):
        setup.install(setup.parser().parse_args([]))
    assert unit.read_text() == original
    assert not (tmp_path / "ibkr-tws-http").exists()


def test_setup_help_does_not_require_uv_or_a_user_manager():
    result = subprocess.run([str(SCRIPT), "--help"], capture_output=True, text=True, check=True)
    assert "--gateway" in result.stdout
    assert "--no-linger" in result.stdout


@pytest.mark.skipif(
    shutil.which("systemd-analyze") is None, reason="requires systemd unit validator"
)
def test_generated_unit_accepts_checkout_paths_with_spaces_and_specifier_characters(tmp_path):
    root = tmp_path / "checkout space %h $HOME"
    (root / "systemd").mkdir(parents=True)
    shutil.copy(setup.ROOT / "systemd/ibkr-tws-http.service.in", root / "systemd")
    executable = root / ".venv/bin/ibkr-tws-http"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    unit = tmp_path / setup.UNIT
    unit.write_text(setup.render_unit(root, root / "http config.toml"))
    subprocess.run(["systemd-analyze", "verify", str(unit)], capture_output=True, check=True)
