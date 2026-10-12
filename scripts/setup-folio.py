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
ZOND_UNIT = "zondd.service"
LEGACY_UNIT = "ibkr-tws-http.service"
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
    source = f"[server]\nport = {port}\n"
    if chromium:
        source += f"\n[browser]\nexecutable = {json.dumps(chromium)}\n"
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
    sys.path.insert(0, str(ROOT / "src"))
    from folio.configure import adapter_config as reconcile

    return reconcile(source, config, connections)


def main(argv=None, *, source=None, expected_original=..., adapter_builder=None):
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
    parser.add_argument("--config", type=Path, help="Folio configuration file")
    parser.add_argument(
        "--auto-migrate",
        action="store_true",
        help="migrate a managed legacy service when installed",
    )
    args = parser.parse_args(argv)
    if os.getuid() == 0 or sys.platform != "linux":
        parser.error("run setup as the ordinary Linux user, without sudo")
    os.umask(0o077)
    home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    if not home.is_absolute():
        parser.error("XDG_CONFIG_HOME must be absolute")
    config = (args.config or home / "folio/config.toml").expanduser().resolve()
    unit = home / "systemd/user" / UNIT
    zond = home / "zondd/config.toml"
    required = ["uv", "systemctl", "systemd-analyze"]
    original = config.read_text() if config.exists() else None
    if expected_original is not ... and original != expected_original:
        parser.error("configuration changed during setup; rerun")
    source = (
        source
        if source is not None
        else original
        or config_source(home, zond=args.zond, chromium=shutil.which("chromium"), port=args.port)
    )
    data = tomllib.loads(source)
    if any(
        c.get("provider") in {"bog", "wolt"} for c in data.get("connections", {}).values()
    ) and not data.get("browser", {}).get("executable"):
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
    previous_unit = unit.read_text() if unit.exists() else None
    old_unit = home / "systemd/user" / LEGACY_UNIT
    old_active = (
        subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", LEGACY_UNIT], check=False
        ).returncode
        == 0
    )
    old_enabled = (
        subprocess.run(
            ["systemctl", "--user", "is-enabled", "--quiet", LEGACY_UNIT], check=False
        ).returncode
        == 0
    )
    if args.auto_migrate and (old_active or old_enabled):
        if not old_unit.exists():
            parser.error("legacy unit is not managed in this config directory; migrate it manually")
        if not any(c.get("provider") == "ibkr" for c in data.get("connections", {}).values()):
            parser.error(
                "legacy IBKR is running but absent from Folio; first run "
                "'folio setup ibkr --yes' to retain its account access"
            )
        args.replace_ibkr_service = True
    if (old_active or old_enabled) and not args.replace_ibkr_service:
        parser.error("legacy service is installed; rerun with --replace-ibkr-service to migrate it")
    if (
        args.replace_ibkr_service
        and old_unit.exists()
        and not old_unit.read_text().startswith(legacy.MARKER)
    ):
        parser.error("legacy unit is custom; migrate it manually")
    if original and args.port is not None and data.get("server", {}).get("port", 8080) != args.port:
        parser.error(
            "existing config is preserved; edit server.port before requesting another port"
        )
    previous_zond = zond.read_text() if zond.exists() else ""
    if args.zond and not previous_zond:
        parser.error("configure zond first; its existing configuration is required")
    candidate = (
        (adapter_builder or adapter_config)(previous_zond, config, data["connections"])
        if args.zond
        else None
    )
    legacy.run(
        "uv",
        "sync",
        "--locked",
        "--no-dev",
        "--inexact",
        cwd=ROOT,
        env={**os.environ, "UV_PROJECT_ENVIRONMENT": str(ROOT / ".venv")},
    )
    unit_source = render_unit(config)
    with tempfile.TemporaryDirectory(prefix="folio-setup-") as directory:
        staged = Path(directory)
        config.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile(
            mode="w", dir=config.parent, prefix=".folio-check-", suffix=".toml"
        ) as check:
            check.write(source)
            check.flush()
            legacy.run(ROOT / ".venv/bin/foliod", "--config", check.name, "--check")
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
    if (unit.read_text() if unit.exists() else None) != previous_unit:
        raise ValueError("Folio service unit changed during setup; rerun")
    prior = {
        name: {
            state: subprocess.run(
                ["systemctl", "--user", "is-" + state, "--quiet", name], check=False
            ).returncode
            == 0
            for state in ("active", "enabled")
        }
        for name in (UNIT, ZOND_UNIT)
    }
    ctl = ["zondctl"]
    if candidate is not None and (runtime := tomllib.loads(candidate).get("runtime_dir")):
        ctl += ["--runtime-dir", runtime]
    if original is not None and source != original:
        with tempfile.NamedTemporaryFile(
            mode="w", prefix="config.toml.backup-", dir=config.parent, delete=False
        ) as backup:
            backup.write(original)
        print(f"Previous configuration: {backup.name}", flush=True)
    try:
        if source != original:
            legacy.write_private(config, source)
        legacy.write_private(unit, unit_source)
        legacy.run("systemctl", "--user", "daemon-reload")
        if args.replace_ibkr_service and (old_active or old_enabled):
            legacy.run("systemctl", "--user", "disable", "--now", LEGACY_UNIT)
        if candidate is not None:
            if candidate != previous_zond:
                legacy.write_private(zond, candidate)
            legacy.run("systemctl", "--user", "enable", "--now", ZOND_UNIT)
            if prior[ZOND_UNIT]["active"]:
                legacy.run(*ctl, "reload")
            expected = {
                name: value
                for name, value in tomllib.loads(candidate).get("credential_adapters", {}).items()
                if name.startswith("folio-")
                and value.get("program") == str(ROOT / ".venv/bin/folio")
            }
            deadline = time.monotonic() + 15
            while expected:
                response = subprocess.run(
                    [*ctl, "--json", "pending"], capture_output=True, check=False, timeout=5
                )
                queue = json.loads(response.stdout) if response.returncode == 0 else {}
                running = {item["recipient"]["adapter"]: item for item in queue.get("adapters", [])}
                if all(
                    name in running
                    and running[name]["state"] in {"ready", "working", "waiting", "unavailable"}
                    and running[name]["recipient"]["command"]
                    == {"program": value["program"], "args": value["args"]}
                    for name, value in expected.items()
                ):
                    break
                if time.monotonic() >= deadline:
                    raise ValueError(
                        "Zond did not start the configured Folio credential relays; "
                        "inspect journalctl --user -u zondd"
                    )
                time.sleep(0.25)
        legacy.run("systemctl", "--user", "enable", unit)
        legacy.run("systemctl", "--user", "restart", UNIT)
        endpoint = f"http://127.0.0.1:{data.get('server', {}).get('port', 8080)}"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                with opener.open(endpoint + "/v1/connections", timeout=1) as response:
                    actual = {item["id"] for item in json.load(response)}
                active = subprocess.run(
                    ["systemctl", "--user", "is-active", "--quiet", UNIT], check=False
                )
                if active.returncode == 0 and actual == set(data["connections"]):
                    break
            except OSError:
                pass
            time.sleep(0.5)
        else:
            raise ValueError("Folio did not become healthy; inspect journalctl --user -u folio")
    except BaseException:
        subprocess.run(["systemctl", "--user", "stop", UNIT], check=False)
        if not prior[UNIT]["enabled"]:
            subprocess.run(["systemctl", "--user", "disable", UNIT], check=False)
        for target, applied, previous in (
            (config, source, original),
            (unit, unit_source, previous_unit),
            (zond, candidate, previous_zond),
        ):
            if applied is None or (target.read_text() if target.exists() else None) != applied:
                continue
            if previous is None:
                target.unlink(missing_ok=True)
            else:
                legacy.write_private(target, previous)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        if candidate is not None:
            if prior[ZOND_UNIT]["active"]:
                subprocess.run([*ctl, "reload"], check=False)
            else:
                subprocess.run(["systemctl", "--user", "stop", ZOND_UNIT], check=False)
            if not prior[ZOND_UNIT]["enabled"]:
                subprocess.run(["systemctl", "--user", "disable", ZOND_UNIT], check=False)
        if prior[UNIT]["enabled"]:
            subprocess.run(["systemctl", "--user", "enable", UNIT], check=False)
        if prior[UNIT]["active"]:
            subprocess.run(["systemctl", "--user", "start", UNIT], check=False)
        if old_enabled:
            subprocess.run(["systemctl", "--user", "enable", LEGACY_UNIT], check=False)
        if old_active:
            subprocess.run(["systemctl", "--user", "start", LEGACY_UNIT], check=False)
        raise
    print(f"Folio is enabled and healthy. Configuration: {config}")
    print(f"CLI: {ROOT}/.venv/bin/folio connections")
    print("Use 'folio connections' to inspect connections and 'folio sync NAME' to fetch data.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError, legacy.SetupError) as error:
        print(f"Setup failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
