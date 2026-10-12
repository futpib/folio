from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("folio_setup", ROOT / "scripts/setup-folio.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


def test_setup_preserves_ibkr_config_and_builds_fixed_adapter_commands(tmp_path):
    legacy = tmp_path / "ibkr-tws-http/config.toml"
    legacy.parent.mkdir()
    original = "[server]\nport=9812\n[tws]\nclient_id=82\n"
    legacy.write_text(original)
    source = setup.config_source(tmp_path, zond=True, chromium="/usr/bin/chromium", port=None)
    parsed = tomllib.loads(source)
    assert parsed["server"]["port"] == 9812
    assert parsed["connections"]["ibkr"]["ibkr_config"] == str(legacy)
    assert legacy.read_text() == original
    config = tmp_path / "folio/config.toml"
    zond = '[principals.test]\npolicy="ask"\n'
    result = setup.adapter_config(zond, config, parsed["connections"])
    assert result.startswith(zond)
    adapters = tomllib.loads(result)["credential_adapters"]
    assert set(adapters) == {"folio-bog", "folio-wolt"}
    assert adapters["folio-bog"]["args"] == ["--config", str(config), "browser-adapter", "bog"]
    assert setup.adapter_config(result, config, parsed["connections"]) == result
    with pytest.raises(ValueError, match="differs"):
        setup.adapter_config(
            result.replace("Folio bog", "Custom bank"), config, parsed["connections"]
        )
    unit = setup.render_unit(config)
    assert "UMask=0077" in unit and "Restart=on-failure" in unit
    assert str(config) in unit


def test_setup_selects_api_adapter_for_binance(tmp_path):
    result = setup.adapter_config(
        "",
        tmp_path / "folio.toml",
        {"binance": {"provider": "binance", "credential_adapter": True}},
    )
    item = tomllib.loads(result)["credential_adapters"]["folio-binance"]
    assert item["args"][-2:] == ["api-adapter", "binance"]


def test_missing_chromium_keeps_default_toml_valid(tmp_path):
    source = setup.config_source(tmp_path, zond=True, chromium=None, port=None)
    assert "browser" not in tomllib.loads(source)


def test_setup_preserves_a_concurrent_service_unit_edit(tmp_path, monkeypatch):
    import json
    import subprocess
    import sys

    config = tmp_path / "folio/config.toml"
    config.parent.mkdir()
    original = (
        f'[browser]\nexecutable={json.dumps(sys.executable)}\n[connections.wolt]\nprovider="wolt"\n'
    )
    config.write_text(original)
    unit = tmp_path / "systemd/user/folio.service"
    unit.parent.mkdir(parents=True)
    unit.write_text(setup.MARKER + "\n[Service]\nExecStart=/usr/bin/true\n")
    changed = "# edited by user during setup\n[Service]\nExecStart=/usr/bin/false\n"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(setup.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(setup.os, "getuid", lambda: 1000)
    monkeypatch.setattr(setup.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1))

    def run(*command, **kwargs):
        if command[0] == "uv":
            unit.write_text(changed)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(setup.legacy, "run", run)
    with pytest.raises(ValueError, match="unit changed"):
        setup.main(["--no-linger"])
    assert unit.read_text() == changed
    assert config.read_text() == original
