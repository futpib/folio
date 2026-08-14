from __future__ import annotations

import ipaddress
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FEATURES = frozenset(
    {
        "status",
        "accounts",
        "account-summary",
        "positions",
        "contract-details",
        "market-data",
        "historical-data",
    }
)

DEFAULT_FEATURES = frozenset(
    {
        "status",
        "accounts",
        "account-summary",
        "positions",
        "contract-details",
        "market-data",
        "historical-data",
    }
)

DEFAULT_ACCOUNT_SUMMARY_TAGS = frozenset(
    {
        "AvailableFunds",
        "BuyingPower",
        "Cushion",
        "ExcessLiquidity",
        "FullAvailableFunds",
        "FullExcessLiquidity",
        "FullInitMarginReq",
        "FullMaintMarginReq",
        "GrossPositionValue",
        "NetLiquidation",
        "TotalCashValue",
    }
)

DEFAULT_SECURITY_TYPES = frozenset({"STK", "OPT", "FUT", "CASH", "IND"})


class ConfigError(ValueError):
    """Raised when configuration is invalid or unsafe."""


def _table(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a table")
    return value


def _string_set(table: dict[str, Any], key: str, default: frozenset[str]) -> frozenset[str]:
    value = table.get(key)
    if value is None:
        return default
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{key} must be an array of strings")
    return frozenset(value)


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8080

    def validate(self) -> None:
        if not self.host:
            raise ConfigError("server.host must not be empty")
        if not 1 <= self.port <= 65535:
            raise ConfigError("server.port must be between 1 and 65535")


@dataclass(frozen=True, slots=True)
class TwsConfig:
    host: str = "127.0.0.1"
    port: int = 7497
    client_id: int = 37
    connect_timeout_seconds: float = 10.0
    request_timeout_seconds: float = 15.0
    reconnect_delay_seconds: float = 5.0

    def validate(self) -> None:
        if not _is_loopback(self.host):
            raise ConfigError("tws.host must be a loopback address")
        if not 1 <= self.port <= 65535:
            raise ConfigError("tws.port must be between 1 and 65535")
        if not 0 <= self.client_id <= 2**31 - 1:
            raise ConfigError("tws.client_id must be a non-negative 32-bit integer")
        for name, value in (
            ("connect_timeout_seconds", self.connect_timeout_seconds),
            ("request_timeout_seconds", self.request_timeout_seconds),
            ("reconnect_delay_seconds", self.reconnect_delay_seconds),
        ):
            if value <= 0:
                raise ConfigError(f"tws.{name} must be positive")


@dataclass(frozen=True, slots=True)
class ExposureConfig:
    features: frozenset[str] = DEFAULT_FEATURES
    allowed_accounts: frozenset[str] = frozenset()
    account_aliases: dict[str, str] = field(default_factory=dict)
    expose_account_ids: bool = False
    account_summary_tags: frozenset[str] = DEFAULT_ACCOUNT_SUMMARY_TAGS
    allowed_security_types: frozenset[str] = DEFAULT_SECURITY_TYPES
    allowed_symbols: frozenset[str] = frozenset()
    allowed_exchanges: frozenset[str] = frozenset()

    def validate(self) -> None:
        unknown = self.features - FEATURES
        if unknown:
            raise ConfigError(f"unknown exposure features: {', '.join(sorted(unknown))}")
        if len(set(self.account_aliases.values())) != len(self.account_aliases):
            raise ConfigError("account aliases must be unique")
        if self.allowed_accounts and not self.account_aliases.keys() <= self.allowed_accounts:
            raise ConfigError("account_aliases contains an account not present in allowed_accounts")
        if not self.account_summary_tags:
            raise ConfigError("account_summary_tags must not be empty")
        invalid_sec_types = {
            value for value in self.allowed_security_types if value != value.upper()
        }
        if invalid_sec_types:
            raise ConfigError("allowed_security_types values must be uppercase")


@dataclass(frozen=True, slots=True)
class Settings:
    server: ServerConfig
    tws: TwsConfig = field(default_factory=TwsConfig)
    exposure: ExposureConfig = field(default_factory=ExposureConfig)

    def validate(self) -> None:
        self.server.validate()
        self.tws.validate()
        self.exposure.validate()

    @classmethod
    def load(cls, path: Path) -> Settings:
        path = path.expanduser().resolve()
        try:
            with path.open("rb") as stream:
                raw = tomllib.load(stream)
        except OSError as exc:
            raise ConfigError(f"cannot read configuration {path}: {exc}") from exc
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"cannot parse configuration {path}: {exc}") from exc

        server_raw = _table(raw, "server")
        tws_raw = _table(raw, "tws")
        exposure_raw = _table(raw, "exposure")

        aliases = exposure_raw.get("account_aliases", {})
        if not isinstance(aliases, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in aliases.items()
        ):
            raise ConfigError("exposure.account_aliases must be a string-to-string table")

        settings = cls(
            server=ServerConfig(
                host=str(server_raw.get("host", "127.0.0.1")),
                port=int(server_raw.get("port", 8080)),
            ),
            tws=TwsConfig(
                host=str(tws_raw.get("host", "127.0.0.1")),
                port=int(tws_raw.get("port", 7497)),
                client_id=int(tws_raw.get("client_id", 37)),
                connect_timeout_seconds=float(tws_raw.get("connect_timeout_seconds", 10)),
                request_timeout_seconds=float(tws_raw.get("request_timeout_seconds", 15)),
                reconnect_delay_seconds=float(tws_raw.get("reconnect_delay_seconds", 5)),
            ),
            exposure=ExposureConfig(
                features=_string_set(exposure_raw, "features", DEFAULT_FEATURES),
                allowed_accounts=_string_set(exposure_raw, "allowed_accounts", frozenset()),
                account_aliases=dict(aliases),
                expose_account_ids=bool(exposure_raw.get("expose_account_ids", False)),
                account_summary_tags=_string_set(
                    exposure_raw, "account_summary_tags", DEFAULT_ACCOUNT_SUMMARY_TAGS
                ),
                allowed_security_types=frozenset(
                    value.upper()
                    for value in _string_set(
                        exposure_raw, "allowed_security_types", DEFAULT_SECURITY_TYPES
                    )
                ),
                allowed_symbols=frozenset(
                    value.upper()
                    for value in _string_set(exposure_raw, "allowed_symbols", frozenset())
                ),
                allowed_exchanges=frozenset(
                    value.upper()
                    for value in _string_set(exposure_raw, "allowed_exchanges", frozenset())
                ),
            ),
        )
        settings.validate()
        return settings
