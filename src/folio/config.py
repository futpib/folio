from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from ibkr_tws_http.config import ConfigError
from ibkr_tws_http.config import Settings as IbkrSettings

RESOURCES = {
    "ibkr": ("accounts", "balances", "positions"),
    "bog": ("accounts", "balances", "transactions"),
    "wolt": ("orders",),
    "binance": ("accounts", "balances", "positions", "trades", "income"),
}


@dataclass(frozen=True)
class BinanceSettings:
    markets: tuple[str, ...] = ("spot", "usdm")
    spot_symbols: tuple[str, ...] = ()
    usdm_symbols: tuple[str, ...] = ()
    coinm_symbols: tuple[str, ...] = ()
    history_days: int = 7
    key_type: str = "auto"

    @classmethod
    def load(cls, data):
        if not isinstance(data, dict) or data.keys() - {
            "markets",
            "spot_symbols",
            "usdm_symbols",
            "coinm_symbols",
            "history_days",
            "key_type",
        }:
            raise ConfigError("unknown Binance settings")
        markets = data.get("markets", ["spot", "usdm"])
        if (
            not isinstance(markets, list)
            or not markets
            or any(not isinstance(m, str) or m not in {"spot", "usdm", "coinm"} for m in markets)
            or len(set(markets)) != len(markets)
        ):
            raise ConfigError("binance.markets must select spot, usdm and/or coinm once each")
        symbols = {}
        for market in ("spot", "usdm", "coinm"):
            key = f"{market}_symbols"
            values = data.get(key, [])
            if (
                not isinstance(values, list)
                or len(values) > 50
                or any(
                    not isinstance(v, str) or not re.fullmatch(r"[A-Z0-9_]{1,40}", v)
                    for v in values
                )
                or len(set(values)) != len(values)
                or (values and market not in markets)
            ):
                raise ConfigError(f"binance.{key} needs unique symbols for an enabled market")
            symbols[key] = tuple(values)
        days = data.get("history_days", 7)
        if type(days) is not int or not 1 <= days <= 90:
            raise ConfigError("binance.history_days must be between 1 and 90")
        key_type = data.get("key_type", "auto")
        if key_type not in ("auto", "hmac", "ed25519", "rsa"):
            raise ConfigError("binance.key_type must be auto, hmac, ed25519 or rsa")
        return cls(markets=tuple(markets), history_days=days, key_type=key_type, **symbols)


def default_config() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "folio/config.toml"


def default_state() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "folio"


@dataclass(frozen=True)
class Connection:
    id: str
    provider: str
    ibkr_config: Path | None = None
    credential_adapter: bool = False
    min_sync_seconds: int = 300
    max_pages: int = 100
    binance: BinanceSettings | None = None

    @property
    def resources(self) -> tuple[str, ...]:
        return RESOURCES[self.provider]


@dataclass(frozen=True)
class Settings:
    state_dir: Path
    connections: tuple[Connection, ...]
    port: int = 8080
    chromium: str | None = None

    @classmethod
    def load(cls, path: Path) -> Settings:
        path = path.expanduser().resolve()
        with path.open("rb") as stream:
            data = tomllib.load(stream)
        return cls.from_data(data, path)

    @classmethod
    def from_data(cls, data: dict, path: Path) -> Settings:
        path = path.expanduser().resolve()
        unknown = data.keys() - {"server", "storage", "browser", "connections"}
        if unknown:
            raise ConfigError(f"unknown Folio configuration sections: {', '.join(sorted(unknown))}")
        for section in ("server", "storage", "browser", "connections"):
            if not isinstance(data.get(section, {}), dict):
                raise ConfigError(f"{section} must be a table")
        for section, allowed in (
            ("server", {"port", "host"}),
            ("storage", {"path"}),
            ("browser", {"executable"}),
        ):
            if data.get(section, {}).keys() - allowed:
                raise ConfigError(f"unknown settings in {section}")
        server = data.get("server", {})
        if server.get("host", "127.0.0.1") != "127.0.0.1":
            raise ConfigError("Folio currently binds only to 127.0.0.1")
        port = server.get("port", 8080)
        if type(port) is not int or not 1 <= port <= 65535:
            raise ConfigError("server.port must be between 1 and 65535")
        state = Path(data.get("storage", {}).get("path", default_state())).expanduser()
        if not state.is_absolute():
            state = path.parent / state
        connections = []
        for name, item in data.get("connections", {}).items():
            if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name):
                raise ConfigError(f"invalid connection name: {name}")
            if not isinstance(item, dict) or item.keys() - {
                "provider",
                "ibkr_config",
                "credential_adapter",
                "min_sync_seconds",
                "max_pages",
                "binance",
            }:
                raise ConfigError(f"unknown configuration for connection {name}")
            provider = item.get("provider")
            if provider not in RESOURCES:
                raise ConfigError(f"unknown provider for {name}")
            if "binance" in item and provider != "binance":
                raise ConfigError("binance settings require the Binance provider")
            binance = (
                BinanceSettings.load(item.get("binance", {})) if provider == "binance" else None
            )
            ibkr_config = None
            if provider == "ibkr":
                if "ibkr_config" not in item:
                    raise ConfigError(
                        f"{name} needs ibkr_config pointing to its IBKR configuration"
                    )
                ibkr_config = Path(item["ibkr_config"]).expanduser()
                if not ibkr_config.is_absolute():
                    ibkr_config = path.parent / ibkr_config
                IbkrSettings.load(ibkr_config)
            for key, default, low, high in (
                ("min_sync_seconds", 300, 1, 86400),
                ("max_pages", 100, 1, 1000),
            ):
                value = item.get(key, default)
                if type(value) is not int or not low <= value <= high:
                    raise ConfigError(f"{name}.{key} must be between {low} and {high}")
            broker = item.get("credential_adapter", False)
            if type(broker) is not bool or (broker and provider == "ibkr"):
                raise ConfigError("credential_adapter is a boolean for browser or API connections")
            if provider == "binance" and not broker:
                raise ConfigError("Binance requires credential_adapter = true for private API keys")
            connections.append(
                Connection(
                    name,
                    provider,
                    ibkr_config,
                    broker,
                    item.get("min_sync_seconds", 300),
                    item.get("max_pages", 100),
                    binance,
                )
            )
        if not connections:
            raise ConfigError("configure at least one [connections.NAME]")
        chromium = data.get("browser", {}).get("executable")
        if chromium is not None and (not isinstance(chromium, str) or not Path(chromium).is_file()):
            raise ConfigError("browser.executable must name an installed Chromium executable")
        return cls(state.resolve(), tuple(connections), port, chromium)

    def connection(self, name: str) -> Connection:
        for connection in self.connections:
            if connection.id == name:
                return connection
        raise ConfigError(f"unknown connection: {name}")
