#!/usr/bin/env python3
"""Install Folio as a persistent user service, preserving the existing IBKR policy."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNIT = "folio.service"
MARKER = "# Managed by scripts/setup-folio.py."
spec = importlib.util.spec_from_file_location("ibkr_setup", ROOT / "scripts/setup-user-service.py")
legacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(legacy)


def config_source(config_home: Path, *, zond: bool, chromium: str, port: int | None) -> str:
    old = config_home / "ibkr-tws-http/config.toml"
    if port is None:
        port = (
            tomllib.loads(old.read_text()).get("server", {}).get("port", 8080)
            if old.exists()
            else 8080
        )
    source = f"[server]\nport = {port}\n\n[browser]\nexecutable = {json.dumps(chromium)}\n"
    if old.exists():
        source += f'\n[connections.ibkr]\nprovider = "ibkr"\nibkr_config = {json.dumps(str(old))}\n'
    for name in ("bog", "wolt"):
        source += (
            f'\n[connections.{name}]\nprovider = "{name}"\n'
            f"credential_adapter = {str(zond).lower()}\n"
        )
    return source


def render_unit(config: Path) -> str:
    return (
        (ROOT / "systemd/folio.service.in")
        .read_text()
        .replace("@EXECUTABLE@", legacy.unit_quote(ROOT / ".venv/bin/foliod"))
        .replace("@CONFIG@", legacy.unit_quote(config, command=True))
    )


def adapter_config(source: str, config: Path, connections: dict) -> str:
    existing = tomllib.loads(source).get("credential_adapters", {})
    for name, connection in connections.items():
        if not connection.get("credential_adapter"):
            continue
        ident = "folio-" + name
        item = {
            "label": f"Folio {connection['provider']} ({name})",
            "program": str(ROOT / ".venv/bin/folio"),
            "args": [
                "--config",
                str(config),
                "api-adapter" if connection["provider"] == "binance" else "browser-adapter",
                name,
            ],
            "cwd": str(ROOT),
        }
        if ident in existing:
            if existing[ident] != item:
                raise ValueError(f"Existing zond adapter {ident} differs; inspect it before setup.")
            continue
        source += f"\n[credential_adapters.{ident}]\n"
        source += "".join(f"{key} = {json.dumps(value)}\n" for key, value in item.items())
    return source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--replace-ibkr-service",
        action="store_true",
        help="replace the legacy HTTP service, retaining its config and Gateway",
    )
    parser.add_argument(
        "--zond", action="store_true", help="broker browser logins and API credentials through zond"
    )
    parser.add_argument("--no-linger", action="store_true")
    parser.add_argument("--port", type=legacy.port)
    args = parser.parse_args()
    if os.getuid() == 0 or sys.platform != "linux":
        parser.error("run setup as the ordinary Linux user, without sudo")
    os.umask(0o077)
    home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    if not home.is_absolute():
        parser.error("XDG_CONFIG_HOME must be absolute")
    config = home / "folio/config.toml"
    unit = home / "systemd/user" / UNIT
    zond = home / "zondd/config.toml"
    required = ["uv", "systemctl", "systemd-analyze"]
    current = tomllib.loads(config.read_text()) if config.exists() else None
    if current is None or any(
        c.get("provider") in {"bog", "wolt"} for c in current.get("connections", {}).values()
    ):
        required.append("chromium")
    if args.zond:
        required += ["zondd", "zondctl"]
    if not args.no_linger:
        required.append("loginctl")
    missing = [name for name in required if shutil.which(name) is None]
    if missing:
        parser.error("missing prerequisites: " + ", ".join(missing))
    legacy.run("systemctl", "--user", "show-environment", stdout=subprocess.DEVNULL)
    if unit.exists() and not unit.read_text().startswith(MARKER):
        parser.error(f"{unit} is not managed by this installer")
    old_unit = home / "systemd/user/ibkr-tws-http.service"
    old_active = (
        subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", "ibkr-tws-http.service"], check=False
        ).returncode
        == 0
    )
    old_enabled = (
        subprocess.run(
            ["systemctl", "--user", "is-enabled", "--quiet", "ibkr-tws-http.service"], check=False
        ).returncode
        == 0
    )
    if (old_active or old_enabled) and not args.replace_ibkr_service:
        parser.error("legacy service is installed; rerun with --replace-ibkr-service to migrate it")
    if (
        args.replace_ibkr_service
        and old_unit.exists()
        and not old_unit.read_text().startswith(legacy.MARKER)
    ):
        parser.error("legacy unit is custom; migrate it manually")
    original = config.read_text() if config.exists() else None
    source = original or config_source(
        home, zond=args.zond, chromium=shutil.which("chromium"), port=args.port
    )
    data = tomllib.loads(source)
    if (
        any(c.get("provider") == "binance" for c in data.get("connections", {}).values())
        and not args.zond
    ):
        parser.error("Binance requires --zond to install its API credential adapter")
    if original and args.port is not None and data.get("server", {}).get("port", 8080) != args.port:
        parser.error(
            "existing config is preserved; edit server.port before requesting another port"
        )
    if (
        original
        and args.zond
        and any(
            not c.get("credential_adapter")
            for c in data["connections"].values()
            if c["provider"] != "ibkr"
        )
    ):
        parser.error(
            "existing config is preserved; set credential_adapter=true before --zond setup"
        )
    previous_zond = zond.read_text() if zond.exists() else ""
    if args.zond and not previous_zond:
        parser.error("configure zond first; its existing configuration is required")
    candidate = adapter_config(previous_zond, config, data["connections"]) if args.zond else None
    legacy.run(
        "uv",
        "sync",
        "--locked",
        "--no-dev",
        cwd=ROOT,
        env={**os.environ, "UV_PROJECT_ENVIRONMENT": str(ROOT / ".venv")},
    )
    unit_source = render_unit(config)
    with tempfile.TemporaryDirectory(prefix="folio-setup-") as directory:
        staged = Path(directory)
        (staged / "config.toml").write_text(source)
        legacy.run(
            ROOT / ".venv/bin/foliod",
            "--config",
            (config if original else staged / "config.toml"),
            "--check",
        )
        (staged / UNIT).write_text(unit_source)
        legacy.run("systemd-analyze", "--user", "verify", staged / UNIT)
        if candidate is not None:
            (staged / "zond.toml").write_text(candidate)
            legacy.run("zondd", "check", "--config", staged / "zond.toml")
    if not args.no_linger:
        legacy.enable_linger()
    if (config.read_text() if config.exists() else None) != original:
        raise ValueError("Folio configuration changed during setup; rerun")
    if candidate is not None and zond.read_text() != previous_zond:
        raise ValueError("zond configuration changed during setup; rerun")
    if original is None:
        legacy.write_private(config, source)
    legacy.write_private(unit, unit_source)
    legacy.run("systemctl", "--user", "daemon-reload")
    try:
        if args.replace_ibkr_service:
            legacy.run("systemctl", "--user", "disable", "--now", "ibkr-tws-http.service")
        if candidate is not None:
            legacy.write_private(zond, candidate)
            ctl = ["zondctl"]
            if runtime := tomllib.loads(candidate).get("runtime_dir"):
                ctl += ["--runtime-dir", runtime]
            legacy.run("systemctl", "--user", "enable", "--now", "zondd.service")
            legacy.run(*ctl, "reload")
        legacy.run("systemctl", "--user", "enable", UNIT)
        legacy.run("systemctl", "--user", "restart", UNIT)
        endpoint = f"http://127.0.0.1:{data.get('server', {}).get('port', 8080)}/healthz"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(30):
            try:
                with opener.open(endpoint, timeout=1) as response:
                    if json.load(response).get("version"):
                        break
            except OSError:
                pass
            time.sleep(0.5)
        else:
            raise ValueError("Folio did not become healthy; inspect journalctl --user -u folio")
    except Exception:
        subprocess.run(["systemctl", "--user", "disable", "--now", UNIT], check=False)
        if candidate is not None:
            legacy.write_private(zond, previous_zond)
            subprocess.run([*ctl, "reload"], check=False)
        if old_enabled:
            subprocess.run(["systemctl", "--user", "enable", "ibkr-tws-http.service"], check=False)
        if old_active:
            subprocess.run(["systemctl", "--user", "start", "ibkr-tws-http.service"], check=False)
        raise
    print(f"Folio is enabled and healthy. Configuration: {config}")
    print(f"CLI: {ROOT}/.venv/bin/folio connections")
    print("Sign in with 'folio login bog' and 'folio login wolt', then run 'folio sync NAME'.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError, legacy.SetupError) as error:
        print(f"Setup failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
