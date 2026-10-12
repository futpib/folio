from __future__ import annotations

import argparse
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import tomlkit
from cryptography.hazmat.primitives import serialization

from folio import configure
from folio.config import Settings
from folio.credentials import read_credentials


def cli(path, *args, success=True):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from pathlib import Path; from folio import credentials; "
            "credentials.DEFAULT_ENV_FILE = Path(sys.argv.pop(1)); "
            "from folio.cli import main; main()",
            str(path.parent / ".env.local"),
            "--config",
            str(path),
            *args,
        ],
        capture_output=True,
        text=True,
        cwd="/",
    )
    assert (result.returncode == 0) == success, result.stderr
    return result


@pytest.mark.parametrize("provider", ["binance", "bog", "wolt", "ibkr"])
def test_setup_without_daemon_for_every_provider_and_preserves_other_settings(
    tmp_path, monkeypatch, provider
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    config = tmp_path / "folio.toml"
    original = (
        "# keep my comments\n[server]\nport = 9901 # chosen port\n"
        '[connections.existing]\nprovider = "wolt"\ncredential_adapter = false\n'
    )
    config.write_text(original)
    args = []
    if provider in {"bog", "wolt"}:
        args = ["--chromium", sys.executable]
    elif provider == "ibkr":
        ibkr = tmp_path / "ibkr.toml"
        ibkr.write_text(
            '[exposure]\nallowed_accounts=["DU123"]\nfeatures=["status", "positions"]\n'
        )
        args = ["--ibkr-config", str(ibkr)]
    else:
        args = ["--markets", "spot,usdm", "--spot-symbols", "ETHUSDT", "--history-days", "30"]
    cli(config, "setup", "personal", "--provider", provider, "--yes", "--configure-only", *args)
    first = config.read_text()
    settings = Settings.load(config)
    assert settings.port == 9901
    assert not settings.connection("existing").credential_adapter
    assert settings.connection("personal").provider == provider
    assert "# keep my comments" in first and "# chosen port" in first
    assert config.stat().st_mode & 0o777 == 0o600
    (backup,) = tmp_path.glob("folio.toml.backup-*")
    assert backup.read_text() == original and backup.stat().st_mode & 0o777 == 0o600
    cli(config, "setup", "personal", "--provider", provider, "--yes", "--configure-only", *args)
    assert config.read_text() == first
    assert len(list(tmp_path.glob("folio.toml.backup-*"))) == 1


def test_fresh_binance_setup_and_generic_config_editor(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    config = tmp_path / "folio.toml"
    cli(config, "setup", "binance", "--yes", "--configure-only")
    data = tomllib.loads(config.read_text())
    assert set(data["connections"]) == {"binance"}
    assert data["connections"]["binance"]["credential_adapter"]
    assert "browser" not in data
    cli(config, "config", "set", "server.port", "9812")
    cli(config, "config", "set", "connections.binance.binance.spot_symbols", '["SOLUSDT"]')
    result = cli(config, "config", "show")
    assert tomllib.loads(result.stdout)["server"]["port"] == 9812
    assert Settings.load(config).connection("binance").binance.spot_symbols == ("SOLUSDT",)
    previous = config.read_bytes()
    cli(config, "config", "set", "server.port", "99999", success=False)
    cli(config, "config", "unset", "connections.binance", success=False)
    cli(config, "config", "set", "server.host", '"0.0.0.0"', success=False)
    cli(config, "config", "set", "server.port", "8080\nother=42", success=False)
    assert config.read_bytes() == previous
    cli(config, "config", "unset", "connections.binance.binance.spot_symbols")
    assert not Settings.load(config).connection("binance").binance.spot_symbols


@pytest.mark.parametrize("key_type", ["auto", "ed25519", "rsa"])
def test_setup_creates_and_reuses_matching_public_key_without_exposing_secrets(
    tmp_path, monkeypatch, key_type
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    path = tmp_path / ".env.local"
    path.write_text("# preserve\nWOLT_PASSWORD='fixture secret'\n")
    config = tmp_path / "folio.toml"
    args = ("setup", "binance", "--yes", "--configure-only", "--key-type", key_type)
    result = cli(config, *args)
    kind = "ed25519" if key_type == "auto" else key_type
    target = tmp_path / f".secrets/binance-{kind}.pem"
    original = target.read_bytes()
    private = serialization.load_pem_private_key(original, None)
    public = (
        private.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    assert public in result.stdout
    assert "Self-generated" in result.stdout and "Zond" in result.stdout
    assert "fixture secret" not in result.stdout + result.stderr
    assert "PRIVATE KEY" not in result.stdout + result.stderr
    assert target.stat().st_mode & 0o777 == path.stat().st_mode & 0o777 == 0o600
    assert target.parent.stat().st_mode & 0o777 == 0o700
    path.write_text(path.read_text() + "BINANCE_API_KEY=FixtureApiKey123456\n")
    credentials = path.read_bytes()
    repeated = cli(config, *args)
    assert public in repeated.stdout and "FixtureApiKey123456" not in repeated.stdout
    assert target.read_bytes() == original and path.read_bytes() == credentials


@pytest.mark.parametrize(
    "values",
    [
        "BINANCE_API_KEY=FixtureApiKey123456\n",
        "BINANCE_API_SECRET=FixtureSecret123456\n",
        "BINANCE_PRIVATE_KEY_FILE=missing.pem\n",
    ],
)
def test_setup_preserves_existing_credentials_and_zond_fallback(tmp_path, monkeypatch, values):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    path = tmp_path / ".env.local"
    path.write_text(values)
    result = cli(tmp_path / "folio.toml", "setup", "binance", "--yes", "--configure-only")
    assert path.read_text() == values and not (tmp_path / ".secrets").exists()
    assert "missing values will be requested through Zond" in result.stdout
    assert "PUBLIC KEY" not in result.stdout


def test_explicit_hmac_setup_does_not_generate_a_key(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    cli(
        tmp_path / "folio.toml",
        "setup",
        "binance",
        "--yes",
        "--configure-only",
        "--key-type",
        "hmac",
    )
    assert not (tmp_path / ".env.local").exists()
    assert not (tmp_path / ".secrets").exists()


def test_existing_zond_only_connection_is_not_silently_given_new_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    config = tmp_path / "folio.toml"
    config.write_text('[connections.binance]\nprovider="binance"\ncredential_adapter=true\n')
    cli(config, "setup", "binance", "--yes", "--configure-only")
    assert not (tmp_path / ".env.local").exists()
    cli(config, "setup", "binance", "--yes", "--configure-only", "--key-type", "ed25519")
    assert (tmp_path / ".secrets/binance-ed25519.pem").is_file()


def test_setup_keeps_encrypted_key_and_defers_missing_passphrase_to_zond(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    config = tmp_path / "folio.toml"
    cli(config, "setup", "binance", "--yes", "--configure-only")
    target = tmp_path / ".secrets/binance-ed25519.pem"
    key = serialization.load_pem_private_key(target.read_bytes(), None)
    encrypted = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(b"FixturePassphrase1234"),
    )
    target.write_bytes(encrypted)
    before = (tmp_path / ".env.local").read_bytes()
    result = cli(config, "setup", "binance", "--yes", "--configure-only")
    assert "Zond will ask for its missing passphrase" in result.stdout
    assert target.read_bytes() == encrypted and (tmp_path / ".env.local").read_bytes() == before
    assert "PRIVATE KEY" not in result.stdout


def test_interactive_setup_prompts_for_provider_and_history(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    parser = argparse.ArgumentParser()
    configure.add_commands(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["setup", "--configure-only"])
    answers = iter(
        [
            "exchange",
            "binance",
            "60",
            "200",
            "spot,usdm",
            "BTCUSDT,ETHUSDT",
            "BTCUSDT",
            "30",
            "ed25519",
            "9888",
            str(tmp_path / "data"),
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    source, settings, name = configure.candidate(
        args, tmp_path / "folio.toml", None, interactive=True
    )
    assert name == "exchange" and settings.port == 9888
    connection = settings.connection(name)
    assert connection.binance.history_days == 30 and connection.binance.key_type == "ed25519"
    assert connection.binance.spot_symbols == ("BTCUSDT", "ETHUSDT")
    assert connection.max_pages == 200 and connection.min_sync_seconds == 60
    assert tomllib.loads(source)["storage"]["path"] == str(tmp_path / "data")


def test_setup_imports_legacy_policy_and_port_without_altering_it(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    old = tmp_path / "ibkr-tws-http/config.toml"
    old.parent.mkdir()
    original = '[server]\nport=9831\n[exposure]\nallowed_accounts=["DU123"]\n'
    old.write_text(original)
    config = tmp_path / "folio/config.toml"
    cli(config, "setup", "binance", "--configure-only", "--yes")
    settings = Settings.load(config)
    assert settings.port == 9831 and settings.connection("ibkr").ibkr_config == old
    assert old.read_text() == original


def test_invalid_setup_leaves_existing_configuration_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    config = tmp_path / "folio.toml"
    cli(config, "setup", "binance", "--configure-only", "--yes")
    original = config.read_bytes()
    for options in (
        ["--provider", "wolt"],
        ["--history-days", "0"],
        ["--markets", "wrong"],
        ["--port", "0"],
    ):
        cli(config, "setup", "binance", "--configure-only", "--yes", *options, success=False)
        assert config.read_bytes() == original
    result = cli(config, "setup", "binance", success=False)
    assert "--yes" in result.stderr


def test_adapter_reconciliation_preserves_policy_and_retires_only_owned_workers(tmp_path):
    original = (
        "# Keep policy and Gateway\n[principals.default]\nallow_exec = false\n"
        '[credential_adapters.ibkr]\nprogram = "/usr/bin/true"\n'
    )
    config = tmp_path / "folio.toml"
    connections = {
        "bank": {"provider": "bog", "credential_adapter": True},
        "binance": {"provider": "binance", "credential_adapter": True},
    }
    source = configure.adapter_config(original, config, connections)
    parsed = tomllib.loads(source)
    assert not parsed["principals"]["default"]["allow_exec"]
    assert parsed["credential_adapters"]["folio-binance"]["args"] == [
        "--config",
        str(config),
        "--watch-config",
        "--worker-revision",
        parsed["credential_adapters"]["folio-binance"]["args"][4],
        "credential-relay",
        "binance",
    ]
    old = tomllib.loads(source)
    del old["credential_adapters"]["folio-binance"]["args"][3:5]
    old["credential_adapters"]["folio-binance"]["args"][3] = "api-adapter"
    old["credential_adapters"]["folio-bank"]["args"][5] = "browser-adapter"
    assert (
        tomllib.loads(configure.adapter_config(tomlkit.dumps(old), config, connections)) == parsed
    )
    assert configure.adapter_config(source, config, connections) == source
    reduced = configure.adapter_config(
        source, config, {"bank": {"provider": "bog", "credential_adapter": False}}
    )
    assert set(tomllib.loads(reduced)["credential_adapters"]) == {"ibkr"}
    assert "# Keep policy and Gateway" in reduced
    with pytest.raises(ValueError, match="custom"):
        configure.adapter_config(
            source.replace('label = "Folio binance (binance)"', 'label = "Custom"'),
            config,
            connections,
        )


def test_credentials_are_masked_private_and_preserve_unrelated_values(
    tmp_path, monkeypatch, capsys
):
    path = tmp_path / ".env.local"
    original = (
        "# keep\nWOLT_PASSWORD='unrelated'\nBINANCE_PRIVATE_KEY_FILE=.secrets/binance-ed25519.pem\n"
    )
    path.write_text(original)
    monkeypatch.setattr(configure.sys.stdin, "isatty", lambda: True)
    answers = iter(["secret $value 'quotes' \\ literal", "", ""])
    monkeypatch.setattr(configure.getpass, "getpass", lambda _: next(answers))
    monkeypatch.setattr("builtins.input", lambda _: "")
    configure.credentials(argparse.Namespace(provider="binance", env_file=path))
    value = read_credentials("binance", path)
    assert value["api_key"] == "secret $value 'quotes' \\ literal"
    assert value["private_key_file"] == ".secrets/binance-ed25519.pem"
    assert "WOLT_PASSWORD='unrelated'" in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600
    output = capsys.readouterr()
    assert "secret $value" not in output.out + output.err
    assert not list(tmp_path.glob("*.backup-*"))


def test_concurrent_edit_is_not_overwritten(tmp_path):
    path = tmp_path / "folio.toml"
    path.write_text("# new edit\n")
    with pytest.raises(ValueError, match="changed"):
        configure.save(path, "# proposed\n", "# original\n")
    assert path.read_text() == "# new edit\n"


def test_real_terminal_credentials_never_echo_secret(tmp_path):
    import errno
    import os
    import pty
    import select
    import time

    path = tmp_path / ".env.local"
    executable = str(Path(sys.executable).parent / "folio")
    pid, terminal = pty.fork()
    if pid == 0:
        os.execv(executable, [executable, "credentials", "binance", "--env-file", str(path)])
    output = bytearray()
    secret = b"FixtureApiKeyOnlyForTerminalTest12345"
    prompts = [
        (b"BINANCE_API_KEY (missing): ", secret + b"\n"),
        (b"BINANCE_API_SECRET (missing): ", b"\n"),
        (b"BINANCE_PRIVATE_KEY_FILE (missing): ", b"\n"),
        (b"BINANCE_PRIVATE_KEY_PASSPHRASE (missing): ", b"\n"),
    ]
    deadline = time.monotonic() + 15
    try:
        while time.monotonic() < deadline:
            if not select.select([terminal], [], [], 0.1)[0]:
                continue
            try:
                chunk = os.read(terminal, 4096)
            except OSError as error:
                if error.errno == errno.EIO:
                    break
                raise
            if not chunk:
                break
            output.extend(chunk)
            if prompts and prompts[0][0] in output:
                _, value = prompts.pop(0)
                os.write(terminal, value)
        else:
            raise AssertionError("credential prompts did not finish")
        _, status = os.waitpid(pid, 0)
        pid = None
        assert os.waitstatus_to_exitcode(status) == 0
        assert not prompts and secret not in output
        assert read_credentials("binance", path)["api_key"].encode() == secret
        assert path.stat().st_mode & 0o777 == 0o600
    finally:
        os.close(terminal)
        if pid is not None:
            os.kill(pid, 15)
            os.waitpid(pid, 0)
