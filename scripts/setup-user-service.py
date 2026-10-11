#!/usr/bin/env python3
"""Install the HTTP user service, optionally with Gateway sign-in through zond."""

from __future__ import annotations

import argparse
import json
import os
import pwd
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNIT = "ibkr-tws-http.service"
MARKER = "# Managed by scripts/setup-user-service.py."


class SetupError(Exception):
    pass


def run(*args: str | Path, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def port(value: str) -> int:
    number = int(value)
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return number


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--tws-port", type=port, help="Gateway/TWS API port (default: 7496)")
    result.add_argument("--http-port", type=port, help="HTTP port (default: 8080)")
    result.add_argument(
        "--gateway", choices=["paper", "live"], help="configure Gateway in zond (ports 4002/4001)"
    )
    result.add_argument("--settings-dir", type=Path, help="existing Gateway settings directory")
    result.add_argument("--gateway-home", type=Path, default=Path("/usr/share/ib-tws"))
    result.add_argument(
        "--no-linger", action="store_true", help="skip boot/logout persistence configuration"
    )
    return result


def unit_quote(path: Path, *, command: bool = False) -> str:
    value = str(path)
    if any(ord(char) < 32 for char in value):
        raise SetupError("installation paths must not contain control characters")
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if command:
        value = value.replace("$", "$$")
    return f'"{value}"'


def render_unit(root: Path, config: Path) -> str:
    if str(root) != str(root).rstrip() or any(ord(char) < 32 for char in str(root)):
        raise SetupError("checkout path must not end in whitespace or contain control characters")
    return (
        (root / "systemd/ibkr-tws-http.service.in")
        .read_text()
        .replace("@CHECKOUT@", str(root).replace("%", "%%"))
        .replace("@EXECUTABLE@", unit_quote(root / ".venv/bin/ibkr-tws-http"))
        .replace("@CONFIG@", unit_quote(config, command=True))
    )


def http_config(path: Path, args: argparse.Namespace) -> str:
    tws_port = args.tws_port
    if path.exists():
        source = path.read_text()
        data = tomllib.loads(source)
        for section, desired, default in [
            ("tws", tws_port, 7496),
            ("server", args.http_port, 8080),
        ]:
            actual = data.get(section, {}).get("port", default)
            if desired is not None and desired != actual:
                raise SetupError(
                    f"{path}: [{section}] port is {actual}, requested {desired}. "
                    "Edit the existing configuration first; setup preserves it."
                )
        return source
    if tws_port is None and args.gateway:
        tws_port = 4002 if args.gateway == "paper" else 4001
    return (
        (ROOT / "systemd/service-config.toml.in")
        .read_text()
        .replace("@TWS_PORT@", str(tws_port or 7496))
        .replace("@HTTP_PORT@", str(args.http_port or 8080))
    )


def gateway_config(path: Path, args: argparse.Namespace, api_port: int) -> str:
    original = path.read_text() if path.exists() else ""
    settings = (args.settings_dir or Path.home() / f".ib-gateway-{args.gateway}").expanduser()
    settings = settings.absolute()
    home = args.gateway_home.expanduser().resolve()
    adapter = {
        "label": f"IBKR Gateway — {args.gateway} account",
        "program": str(ROOT / "adapters/ibgateway/launch.py"),
        "args": [
            "--gateway-home",
            str(home),
            "--settings-dir",
            str(settings),
            "--mode",
            args.gateway,
            "--api-port",
            str(api_port),
        ],
        "cwd": str(Path.home()),
    }
    existing = tomllib.loads(original).get("credential_adapters", {}).get("ibkr")
    if existing is not None:
        if any(existing.get(key) != adapter[key] for key in ("program", "args", "cwd")):
            raise SetupError(
                f"{path}: credential_adapters.ibkr already has different settings. "
                "Review that entry manually before rerunning setup."
            )
        return original
    entry = "\n\n[credential_adapters.ibkr]\n" + "".join(
        f"{key} = {json.dumps(value, ensure_ascii=False)}\n" for key, value in adapter.items()
    )
    candidate = original + entry
    tomllib.loads(candidate)
    return candidate


def write_private(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            file.write(content)
            file.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def enable_linger() -> None:
    user = pwd.getpwuid(os.getuid()).pw_name
    state = run("loginctl", "show-user", user, "--property=Linger", "--value", capture_output=True)
    if state.stdout.strip() == b"yes":
        return
    result = subprocess.run(["loginctl", "enable-linger", user], check=False)
    if result.returncode:
        raise SetupError(
            f"For boot/logout persistence, run sudo loginctl enable-linger {shlex.quote(user)}, "
            "then rerun setup. Use --no-linger only for a login-session service."
        )


def configure_gateway(path: Path, candidate: str, original: str | None) -> None:
    if (path.read_text() if path.exists() else None) != original:
        raise SetupError(f"{path} changed during setup; rerun to preserve the newer configuration.")
    changed = candidate != original
    if changed and original is not None:
        with tempfile.NamedTemporaryFile(
            prefix="config.toml.backup-", dir=path.parent, delete=False
        ) as f:
            backup = Path(f.name)
        backup.write_text(original)
        print(f"Saved existing zond configuration: {backup}", flush=True)
    if changed:
        write_private(path, candidate)
    config_data = tomllib.loads(candidate)
    ctl = ["zondctl"]
    if runtime := config_data.get("runtime_dir"):
        ctl += ["--runtime-dir", runtime]
    was_active = (
        subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", "zondd.service"], check=False
        ).returncode
        == 0
    )
    applied = False
    try:
        run("systemctl", "--user", "enable", "zondd.service")
        if was_active:
            run(*ctl, "reload")
        else:
            run("systemctl", "--user", "start", "zondd.service")
        applied = True
        expected = config_data["credential_adapters"]["ibkr"]
        deadline = time.monotonic() + 15
        while True:
            response = subprocess.run(
                [*ctl, "--json", "pending"], capture_output=True, check=False, timeout=5
            )
            queue = json.loads(response.stdout) if response.returncode == 0 else {}
            if any(
                item["recipient"]["adapter"] == "ibkr"
                and item["recipient"]["command"]
                == {"program": expected["program"], "args": expected["args"]}
                for item in queue.get("adapters", [])
            ):
                break
            if time.monotonic() >= deadline:
                raise SetupError("Running zond did not load the configured Gateway adapter")
            time.sleep(0.25)
    except (subprocess.SubprocessError, OSError, ValueError, SetupError) as error:
        if changed:
            if original is None:
                path.unlink()
            else:
                write_private(path, original)
        if not was_active:
            subprocess.run(["systemctl", "--user", "stop", "zondd.service"], check=False)
        elif applied and changed:
            subprocess.run([*ctl, "reload"], check=False)
        raise SetupError(
            f"Could not apply Gateway configuration: {error}. "
            "Restored the previous zond configuration. "
            "Check journalctl --user -u zondd.service. Both the installed and running zond "
            "must support credential adapters. The HTTP service is installed separately."
        ) from None


def install(args: argparse.Namespace) -> None:
    if sys.platform != "linux" or os.getuid() == 0:
        raise SetupError("Run setup as your ordinary Linux login user, without sudo.")
    if args.settings_dir and not args.gateway:
        raise SetupError("--settings-dir requires --gateway paper or --gateway live")
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    if not config_home.is_absolute():
        raise SetupError("XDG_CONFIG_HOME must be an absolute path")
    config = config_home / "ibkr-tws-http/config.toml"
    unit = config_home / "systemd/user" / UNIT
    zond_config = config_home / "zondd/config.toml"
    required = ["uv", "systemctl", "systemd-analyze"]
    if not args.no_linger:
        required.append("loginctl")
    if args.gateway:
        required += ["javac", "jar", "xvfb-run", "xauth", "zondd", "zondctl"]
    missing = [name for name in required if shutil.which(name) is None]
    if missing:
        raise SetupError(
            f"Missing prerequisites: {', '.join(missing)}. See README.md setup instructions."
        )
    run("systemctl", "--user", "show-environment", stdout=subprocess.DEVNULL)
    if unit.exists() and not unit.read_text().startswith(MARKER):
        raise SetupError(f"{unit} was installed manually. Back it up and remove it before setup.")
    source = http_config(config, args)
    original_config = config.read_text() if config.exists() else None
    original_zond = zond_config.read_text() if zond_config.exists() else None
    data = tomllib.loads(source)
    api_port = data.get("tws", {}).get("port", 7496)
    candidate = gateway_config(zond_config, args, api_port) if args.gateway else None
    unit_source = render_unit(ROOT, config)
    if args.gateway:
        gateway_home = args.gateway_home.expanduser().resolve()
        if not (gateway_home / "jre/bin/java").is_file() or not (gateway_home / "jars").is_dir():
            raise SetupError(f"Install ib-tws first: no Gateway installation at {gateway_home}")
        run(ROOT / "adapters/ibgateway/build.sh", gateway_home)
    run(
        "uv",
        "sync",
        "--locked",
        "--no-dev",
        cwd=ROOT,
        env={**os.environ, "UV_PROJECT_ENVIRONMENT": str(ROOT / ".venv")},
    )
    executable = ROOT / ".venv/bin/ibkr-tws-http"
    with tempfile.TemporaryDirectory(prefix="ibkr-service-setup-") as directory:
        staging = Path(directory)
        check_config = staging / "http.toml"
        check_config.write_text(source)
        run(executable, "--config", check_config, "--check")
        check_unit = staging / UNIT
        check_unit.write_text(unit_source)
        run("systemd-analyze", "--user", "verify", check_unit)
        if candidate is not None:
            check_zond = staging / "zond.toml"
            check_zond.write_text(candidate)
            check_zond.chmod(0o600)
            run("zondd", "check", "--config", check_zond)
    if not args.no_linger:
        enable_linger()
    if (config.read_text() if config.exists() else None) != original_config:
        raise SetupError(
            f"{config} changed during setup; rerun to preserve the newer configuration."
        )
    if not config.exists():
        write_private(config, source)
    write_private(unit, unit_source)
    run("systemctl", "--user", "daemon-reload")
    run("systemctl", "--user", "enable", UNIT)
    run("systemctl", "--user", "restart", UNIT)
    if candidate is not None:
        configure_gateway(zond_config, candidate, original_zond)
    host = data.get("server", {}).get("host", "127.0.0.1")
    http_port = data.get("server", {}).get("port", 8080)
    cli = ROOT / ".venv/bin/ibkr-tws"
    deadline = time.monotonic() + 15
    while True:
        health = subprocess.run(
            [str(cli), "--host", host, "--port", str(http_port), "--timeout", "1", "health"],
            capture_output=True,
            check=False,
        )
        active = subprocess.run(["systemctl", "--user", "is-active", "--quiet", UNIT], check=False)
        if active.returncode == 0 and health.returncode == 0:
            break
        if time.monotonic() >= deadline:
            raise SetupError(
                f"HTTP service did not become healthy. Run journalctl --user -u {UNIT}."
            )
        time.sleep(0.25)
    print(f"HTTP service enabled and healthy. Configuration: {config}")
    print(f"Check Gateway/TWS connection: {shlex.quote(str(cli))} --port {http_port} status")
    print(f"Logs: journalctl --user -u {UNIT} -f")
    if args.gateway:
        print("Gateway sign-in: open zond's Approvals screen, then complete IBKR Mobile approval.")
        print(
            f"Gateway's configured API port must be {api_port}; "
            "setup does not change its API settings."
        )


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        install(args)
    except (SetupError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Setup failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
