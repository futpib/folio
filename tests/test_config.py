from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from ibkr_tws_http.config import ConfigError, ServerConfig, Settings, TwsConfig
from ibkr_tws_http.main import load_settings, main


def test_config_loads_policy_and_cli_overrides_bind(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        """
[server]
host = "127.0.0.2"
port = 8000

[exposure]
features = ["status", "positions"]
allowed_accounts = ["DU123"]
account_aliases = { DU123 = "paper" }
allowed_security_types = ["stk"]
allowed_symbols = ["aapl"]
""".strip()
    )
    args = Namespace(config=config, host="0.0.0.0", port=9000, tws_port=4001)
    settings = load_settings(args)
    assert settings.server.host == "0.0.0.0"
    assert settings.server.port == 9000
    assert settings.tws.port == 4001
    assert settings.exposure.features == frozenset({"status", "positions"})
    assert settings.exposure.account_aliases == {"DU123": "paper"}
    assert settings.exposure.allowed_security_types == frozenset({"STK"})
    assert settings.exposure.allowed_symbols == frozenset({"AAPL"})


def test_tws_host_must_remain_local() -> None:
    with pytest.raises(ConfigError, match="loopback"):
        TwsConfig(host="10.0.0.5").validate()


def test_default_tws_port_is_live_trading_port() -> None:
    assert Settings(server=ServerConfig()).tws.port == 7496


def test_check_works_without_official_client(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--host", "127.0.0.1", "--port", "9080", "--check"]) == 0
    assert capsys.readouterr().out == "configuration is valid\n"
