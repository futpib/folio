"""Offline configuration and service setup for every Folio provider."""

from __future__ import annotations

import getpass
import hashlib
import importlib.util
import os
import re
import shutil
import sys
import tempfile
import tomllib
from pathlib import Path

import tomlkit

from .config import RESOURCES, Settings
from .credentials import FIELDS, credential_path, read_credentials
from .models import LoginRequired
from .store import exclusive

ROOT = Path(__file__).resolve().parents[2]


def csv(value):
    return [item.strip() for item in value.split(",") if item.strip()]


def add_commands(sub):
    setup = sub.add_parser("setup", help="configure any connection and start Folio on this machine")
    setup.add_argument("name", nargs="?", help="existing connection name, or a new name")
    setup.add_argument("--provider", choices=sorted(RESOURCES))
    setup.add_argument(
        "--yes", action="store_true", help="use defaults without interactive questions"
    )
    setup.add_argument(
        "--configure-only", action="store_true", help="save without changing services"
    )
    setup.add_argument(
        "--no-linger", action="store_true", help="skip boot/logout persistence setup"
    )
    setup.add_argument("--port", type=int, help="local HTTP port")
    setup.add_argument("--storage", type=Path, help="data and browser-profile directory")
    setup.add_argument("--chromium", type=Path, help="browser executable for BoG/Wolt")
    setup.add_argument(
        "--ibkr-config", type=Path, help="existing Gateway/TWS config and exposure policy"
    )
    setup.add_argument("--browser-auth", choices=["zond", "browser"])
    setup.add_argument("--min-sync-seconds", type=int)
    setup.add_argument("--max-pages", type=int)
    setup.add_argument("--markets", type=csv, help="Binance markets: spot,usdm,coinm")
    for market in ("spot", "usdm", "coinm"):
        setup.add_argument(f"--{market}-symbols", type=csv, help="comma-separated trade symbols")
    setup.add_argument("--history-days", type=int)
    setup.add_argument("--key-type", choices=["auto", "hmac", "ed25519", "rsa"])
    config = sub.add_parser(
        "config", help="inspect or edit validated configuration without a daemon"
    )
    commands = config.add_subparsers(dest="config_command", required=True)
    commands.add_parser("show", help="show configuration; credentials remain in .env.local")
    setting = commands.add_parser("set", help="set a dotted configuration key to a TOML value")
    setting.add_argument(
        "key", help="for example server.port or connections.binance.binance.markets"
    )
    setting.add_argument("value", help='TOML literal, e.g. 8081, true, or ["spot", "usdm"]')
    remove = commands.add_parser("unset", help="remove a setting or connection, then validate")
    remove.add_argument("key")
    credentials = sub.add_parser(
        "credentials", help="securely edit local credentials without CLI secrets"
    )
    credentials.add_argument("provider", choices=sorted(RESOURCES))
    credentials.add_argument("--env-file", type=Path)


def table(parent, name):
    if name not in parent:
        parent[name] = tomlkit.table()
    if not isinstance(parent[name], dict):
        raise ValueError(f"{name} must be a TOML table")
    return parent[name]


def ask(label, default="", *, convert=str, choices=None):
    while True:
        raw = input(f"{label} [{default}]: ").strip()
        value = raw if raw else default
        try:
            value = convert(str(value))
            if choices is not None and value not in choices:
                raise ValueError("choose " + ", ".join(choices))
            return value
        except ValueError as error:
            print(f"Invalid value: {error}", file=sys.stderr)


def private_write(path, source):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(source)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def save(path, source, original):
    if (path.read_text() if path.exists() else None) != original:
        raise ValueError(
            "Configuration changed during setup; rerun to preserve the newer settings."
        )
    if source != original:
        if original is not None:
            fd, backup = tempfile.mkstemp(prefix=f"{path.name}.backup-", dir=path.parent)
            with os.fdopen(fd, "w") as stream:
                stream.write(original)
            print(f"Previous configuration: {backup}")
        private_write(path, source)


def candidate(args, path, original, *, interactive):
    doc = tomlkit.parse(original or "")
    home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    legacy = home / "ibkr-tws-http/config.toml"
    if original is None and legacy.is_file():
        legacy_data = tomllib.loads(legacy.read_text())
        table(doc, "server")["port"] = legacy_data.get("server", {}).get("port", 8080)
        ibkr = table(table(doc, "connections"), "ibkr")
        ibkr.update(provider="ibkr", ibkr_config=str(legacy.resolve()))
    connections = table(doc, "connections")
    name = args.name
    if interactive and not name:
        if connections:
            print("Existing connections: " + ", ".join(connections))
        name = ask("Connection to configure (existing or new)", next(iter(connections), "binance"))
    elif not name and args.provider:
        name = args.provider
    if name is not None:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name):
            raise ValueError(
                "Connection names must start with a lowercase letter and use a-z, 0-9, _ or -."
            )
        entry = table(connections, name)
        provider = args.provider or entry.get("provider") or (name if name in RESOURCES else None)
        if interactive:
            provider = ask(
                "Provider (binance, bog, wolt, ibkr)", provider or "binance", choices=RESOURCES
            )
        if not provider:
            raise ValueError("Choose --provider binance, bog, wolt or ibkr for the new connection.")
        if entry.get("provider", provider) != provider:
            raise ValueError(
                "Use a new connection name when changing providers; existing data is preserved."
            )
        entry["provider"] = provider
        binance_fields = (
            "markets",
            "spot_symbols",
            "usdm_symbols",
            "coinm_symbols",
            "history_days",
            "key_type",
        )
        if provider != "binance" and any(getattr(args, key) is not None for key in binance_fields):
            raise ValueError(
                "Binance market, symbol, history and key options require the Binance provider."
            )
        if provider != "ibkr" and args.ibkr_config is not None:
            raise ValueError("--ibkr-config requires the IBKR provider.")
        if provider not in {"bog", "wolt"} and args.browser_auth is not None:
            raise ValueError("--browser-auth requires BoG or Wolt.")
        for key, label, default in (
            ("min_sync_seconds", "Minimum seconds between syncs", 300),
            ("max_pages", "Maximum pages per sync", 100),
        ):
            value = getattr(args, key)
            if interactive and value is None:
                value = ask(label, entry.get(key, default), convert=int)
            if value is not None:
                entry[key] = value
        if provider == "ibkr":
            value = args.ibkr_config or entry.get("ibkr_config") or legacy
            if args.ibkr_config is None and not Path(value).expanduser().is_absolute():
                value = path.parent / value
            if interactive and args.ibkr_config is None:
                value = ask("Existing IBKR configuration (includes exposure policy)", value)
            if args.ibkr_config is not None or interactive or "ibkr_config" not in entry:
                entry["ibkr_config"] = str(Path(value).expanduser().resolve())
        else:
            auth = args.browser_auth or (
                "zond" if entry.get("credential_adapter", True) else "browser"
            )
            if interactive and provider in {"bog", "wolt"} and args.browser_auth is None:
                auth = ask("Login method (zond or browser)", auth, choices=["zond", "browser"])
            entry["credential_adapter"] = provider == "binance" or auth == "zond"
        if provider == "binance":
            options = table(entry, "binance")
            markets = args.markets
            if interactive and markets is None:
                markets = ask(
                    "Markets (spot,usdm,coinm)",
                    ",".join(options.get("markets", ["spot", "usdm"])),
                    convert=csv,
                )
            if markets is not None:
                options["markets"] = markets
            elif "markets" not in options:
                options["markets"] = ["spot", "usdm"]
            for market in ("spot", "usdm", "coinm"):
                key = market + "_symbols"
                value = getattr(args, key)
                if markets is not None and market not in markets and value is None:
                    options.pop(key, None)
                if interactive and value is None and market in options["markets"]:
                    value = ask(
                        f"{market} trade symbols (comma-separated; '-' clears)",
                        ",".join(options.get(key, [])),
                        convert=lambda v: [] if v == "-" else csv(v),
                    )
                if value is not None:
                    options[key] = value
            for key, label, default, convert in (
                ("history_days", "History lookback in days (1-90)", 7, int),
                ("key_type", "Signing key type (auto, ed25519, rsa, hmac)", "auto", str),
            ):
                value = getattr(args, key)
                if interactive and value is None:
                    value = ask(label, options.get(key, default), convert=convert)
                if value is not None:
                    options[key] = value
        if (
            provider in {"bog", "wolt"}
            and not doc.get("browser", {}).get("executable")
            and args.chromium is None
        ):
            executable = shutil.which("chromium") or shutil.which("chromium-browser")
            if interactive:
                executable = ask("Chromium executable", executable or "")
            if not executable:
                raise ValueError(
                    "Install Chromium or pass --chromium /path/to/chromium for browser connections."
                )
            table(doc, "browser")["executable"] = executable
    else:
        specific = (
            "ibkr_config",
            "browser_auth",
            "markets",
            "spot_symbols",
            "usdm_symbols",
            "coinm_symbols",
            "history_days",
            "key_type",
            "min_sync_seconds",
            "max_pages",
        )
        if any(getattr(args, key) is not None for key in specific):
            raise ValueError("Supply a connection name for connection-specific options.")
        if not connections:
            raise ValueError(
                "Choose a connection, e.g. 'folio setup binance --yes', or run interactively."
            )
    for key, section, field, label, default, convert in (
        ("port", "server", "port", "Local HTTP port", 8080, int),
        (
            "storage",
            "storage",
            "path",
            "Data and browser profiles directory",
            str(Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "folio"),
            str,
        ),
        ("chromium", "browser", "executable", "Chromium executable", "", str),
    ):
        value = getattr(args, key)
        if interactive and value is None and key != "chromium":
            current = doc.get(section, {}).get(field, default)
            if key == "storage" and not Path(current).expanduser().is_absolute():
                current = path.parent / current
            value = ask(label, current, convert=convert)
        if value is not None:
            table(doc, section)[field] = (
                value if key == "port" else str(Path(value).expanduser().resolve())
            )
    source = tomlkit.dumps(doc)
    settings = Settings.from_data(tomllib.loads(source), path)
    return source, settings, name


def installer():
    script = ROOT / "scripts/setup-folio.py"
    if not script.is_file():
        raise ValueError(
            "Service setup needs the Folio checkout. Use --configure-only to save configuration."
        )
    spec = importlib.util.spec_from_file_location("folio_installer", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def adapter_config(source, config, connections):
    doc = tomlkit.parse(source)
    adapters = table(doc, "credential_adapters")
    desired = {}
    revision = hashlib.sha256()
    for path in [*sorted((ROOT / "src/folio").glob("*.py")), ROOT / "uv.lock"]:
        revision.update(path.name.encode())
        revision.update(path.read_bytes())
    for ident, connection in connections.items():
        if connection.get("credential_adapter"):
            desired["folio-" + ident] = {
                "label": f"Folio {connection['provider']} ({ident})",
                "program": str(ROOT / ".venv/bin/folio"),
                "args": [
                    "--config",
                    str(config),
                    "--watch-config",
                    "--worker-revision",
                    revision.hexdigest(),
                    "credential-relay",
                    ident,
                ],
                "cwd": str(ROOT),
            }
    for name, item in list(adapters.items()):
        if not name.startswith("folio-"):
            continue
        ident = name.removeprefix("folio-")
        command = list(item.get("args", []))
        if (
            len(command) == 7
            and command[3] == "--worker-revision"
            and isinstance(command[4], str)
            and re.fullmatch(r"[a-f0-9]{64}", command[4])
        ):
            del command[3:5]
        if len(command) == 5 and command[2] == "--watch-config":
            del command[2]
        owned = (
            set(item) == {"label", "program", "args", "cwd"}
            and item.get("program") == str(ROOT / ".venv/bin/folio")
            and item.get("cwd") == str(ROOT)
            and item.get("label") in {f"Folio {provider} ({ident})" for provider in RESOURCES}
            and len(command) == 4
            and command[:2] == ["--config", str(config)]
            and command[2] in {"api-adapter", "browser-adapter", "credential-relay"}
            and command[3] == ident
        )
        if name in desired and item != desired[name] and not owned:
            raise ValueError(f"Existing zond adapter {name} is custom; it was not replaced.")
        if owned and name not in desired:
            del adapters[name]
    for name, item in desired.items():
        if name not in adapters or adapters[name] != item:
            adapters[name] = item
    return tomlkit.dumps(doc)


def credentials(args):
    if not sys.stdin.isatty():
        raise ValueError(
            "Run 'folio credentials' in a terminal; secrets are never accepted as arguments."
        )
    path = credential_path(args.env_file).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with exclusive(path.with_name(path.name + ".lock")):
        original = path.read_text() if path.exists() else None
        existing = read_credentials(args.provider, path)
        lines = (original or "").splitlines()
        print("Enter keeps the current value; '-' removes it so Zond can ask when needed.")
        if args.provider == "binance":
            print("Signing key setup is included in 'folio setup binance'.")
        for field in FIELDS[args.provider]:
            if field == "private_key":
                continue
            name = f"{args.provider}_{field}".upper()
            prompt = f"{name} ({'configured' if field in existing else 'missing'}): "
            value = input(prompt) if field == "private_key_file" else getpass.getpass(prompt)
            if not value:
                continue
            if "\x00" in value or "\n" in value or "\r" in value or len(value) > 4096:
                raise ValueError("Credential must be a single line of at most 4096 characters.")
            lines = [
                line for line in lines if not re.match(rf"\s*(?:export\s+)?{name}\s*(?:=|$)", line)
            ]
            if value != "-":
                escaped = value.replace("\\", "\\\\").replace("'", "\\'")
                lines.append(f"{name}='{escaped}'")
        if (path.read_text() if path.exists() else None) != original:
            raise ValueError("Credential file changed during editing; it was not overwritten.")
        private_write(path, "\n".join(lines) + "\n")
    print(f"Credentials saved privately in {path}. Missing values still use Zond.")


def binance_setup(connection, *, create):
    from .binance_keys import PassphraseRequired, setup_public_key

    try:
        public = setup_public_key(connection.binance.key_type, create=create)
    except PassphraseRequired:
        public = None
        print("Existing encrypted signing key retained; Zond will ask for its missing passphrase.")
    except LoginRequired as error:
        raise ValueError(str(error)) from None
    if public:
        print("Binance public key for this machine (existing private keys are reused):")
        print(public, end="")
        print(
            "For a new Binance API key, choose Self-generated and paste the entire public "
            "key block above. For an existing API key, check that it uses this public key."
        )
    else:
        print(
            "Binance signing credentials retained; missing values will be requested through Zond."
        )
    print(
        "API credentials are read from .env.local first. On sync, enter only missing values "
        "in Zond. To save the API key locally, use 'folio credentials binance'.",
        flush=True,
    )


def run(args):
    if args.command == "credentials":
        credentials(args)
        return
    path = args.config.expanduser().resolve()
    if args.command == "config" and args.config_command == "show":
        print(path.read_text(), end="")
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with exclusive(path.with_name(path.name + ".lock")):
        original = path.read_text() if path.exists() else None
        if args.command == "config":
            doc = tomlkit.parse(original or "")
            keys = args.key.split(".")
            if any(not re.fullmatch(r"[a-z][a-z0-9_-]*", key) for key in keys):
                raise ValueError("Use a dotted configuration key such as server.port.")
            target = doc
            for key in keys[:-1]:
                target = table(target, key)
            if args.config_command == "unset":
                if keys[-1] not in target:
                    raise ValueError("That setting does not exist.")
                del target[keys[-1]]
            else:
                parsed = tomlkit.parse("value = " + args.value)
                if set(parsed) != {"value"}:
                    raise ValueError("Supply one TOML value.")
                target[keys[-1]] = parsed["value"]
            source = tomlkit.dumps(doc)
            Settings.from_data(tomllib.loads(source), path)
            for name, old in tomllib.loads(original or "").get("connections", {}).items():
                new = doc.get("connections", {}).get(name)
                if new is not None and new.get("provider") != old.get("provider"):
                    raise ValueError("Use a new connection name when changing providers.")
            save(path, source, original)
            print("Configuration saved. Run 'folio setup --yes' to apply it to services.")
            return
        interactive = not args.yes
        if interactive and not sys.stdin.isatty():
            raise ValueError(
                "Run setup in a terminal, or add --yes to use defaults and explicit options."
            )
        source, settings, name = candidate(args, path, original, interactive=interactive)
        print(f"Configuring Folio on {os.uname().nodename}: {path}", flush=True)
        for connection in settings.connections:
            print(f"  {connection.id}: {connection.provider}", flush=True)
        selected = [settings.connection(name)] if name else settings.connections
        existing = tomllib.loads(original or "").get("connections", {})
        for connection in selected:
            if connection.provider == "binance":
                binance_setup(
                    connection,
                    create=connection.id not in existing or args.key_type in {"ed25519", "rsa"},
                )
        if args.configure_only:
            save(path, source, original)
            print("Configuration saved; services were not changed.")
        else:
            command = ["--config", str(path), "--auto-migrate"]
            zond_path = (
                Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
                / "zondd/config.toml"
            )
            if zond_path.exists() and shutil.which("zondd") and shutil.which("zondctl"):
                command.append("--zond")
            elif any(c.credential_adapter for c in settings.connections):
                print(
                    "Zond is unavailable; local credentials still work. "
                    "Missing fields need Zond setup."
                )
            if args.no_linger:
                command.append("--no-linger")
            module = installer()
            try:
                module.main(
                    command,
                    source=source,
                    expected_original=original,
                    adapter_builder=adapter_config,
                )
            except module.legacy.SetupError as error:
                raise ValueError(str(error)) from None
        if name:
            connection = settings.connection(name)
            if connection.provider != "ibkr":
                print(
                    f"Credentials: folio credentials {connection.provider} "
                    "(or leave missing fields for Zond)."
                )
            if connection.provider == "binance" and not any(
                getattr(connection.binance, m + "_symbols") for m in connection.binance.markets
            ):
                print(
                    "Trade symbols are empty: balances, positions and income work; "
                    "per-symbol trades are skipped."
                )
            print(f"Next: folio sync {name}")
