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
