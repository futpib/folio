import asyncio
import base64
import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from binance_fixture import KEY, SECRET, Api
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from test_binance import Broker, connection

from folio.binance import BinanceConnector
from folio.binance_keys import Credentials, generate_key, load_private_key, read_binance_credentials
from folio.credentials import CredentialFileError, read_credentials
from folio.models import LoginRequired


@pytest.mark.parametrize("kind", ["ed25519", "rsa"])
def test_cli_generates_private_key_once_and_only_prints_public_key(tmp_path, kind):
    env_file = tmp_path / ".env.local"
    env_file.write_text("# preserve\nBOG_PASSWORD='fixture $password'\nBINANCE_API_KEY=fixture\n")
    cli = Path(sys.executable).parent / "folio"
    result = subprocess.run(
        [str(cli), "binance-keygen", "--type", kind, "--env-file", str(env_file)],
        cwd="/",
        capture_output=True,
        check=True,
    )
    private_file = tmp_path / f".secrets/binance-{kind}.pem"
    pem = private_file.read_bytes()
    private = serialization.load_pem_private_key(pem, None)
    public = serialization.load_pem_public_key(result.stdout)
    assert isinstance(
        private, ed25519.Ed25519PrivateKey if kind == "ed25519" else rsa.RSAPrivateKey
    )
    assert public.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    ) == private.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    assert result.stderr == b"" and b"PRIVATE" not in result.stdout
    assert private_file.stat().st_mode & 0o777 == env_file.stat().st_mode & 0o777 == 0o600
    assert private_file.parent.stat().st_mode & 0o777 == 0o700
    assert (
        "# preserve\nBOG_PASSWORD='fixture $password'\nBINANCE_API_KEY=fixture\n"
        in env_file.read_text()
    )
    assert (
        read_credentials("binance", env_file)["private_key_file"] == f".secrets/{private_file.name}"
    )
    assert generate_key(kind, env_file=env_file).encode() == result.stdout
    assert private_file.read_bytes() == pem
    assert "PRIVATE KEY" not in env_file.read_text()


def test_keygen_never_overwrites_registered_or_broken_keys(tmp_path):
    path = tmp_path / ".env.local"
    path.write_text("BINANCE_PRIVATE_KEY_FILE=existing.pem\n")
    with pytest.raises(CredentialFileError, match="not replaced"):
        generate_key(env_file=path)
    assert path.read_text() == "BINANCE_PRIVATE_KEY_FILE=existing.pem\n"
    path.unlink()
    target = tmp_path / ".secrets/binance-ed25519.pem"
    target.write_bytes(b"fixture-broken-key")
    with pytest.raises(CredentialFileError, match="not replaced"):
        generate_key(env_file=path)
    assert target.read_bytes() == b"fixture-broken-key"
    assert not path.exists()


@pytest.fixture(params=["ed25519", "rsa", "rsa4096"])
def signing_key(request):
    return (
        ed25519.Ed25519PrivateKey.generate()
        if request.param == "ed25519"
        else rsa.generate_private_key(
            public_exponent=65537, key_size=4096 if request.param == "rsa4096" else 2048
        )
    )


def pem(key, password=None):
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(password.encode())
        if password
        else serialization.NoEncryption(),
    ).decode()


def test_signatures_verified_by_openssl_and_all_markets(signing_key, tmp_path):
    credential = Credentials.from_values({"api_key": KEY, "private_key": pem(signing_key)})
    payload = "symbol=BTCUSDT&note=%2B%2F%3D%26&timestamp=1792000000000&recvWindow=5000"
    (tmp_path / "message").write_text(payload)
    (tmp_path / "signature").write_bytes(base64.b64decode(credential.sign(payload), validate=True))
    (tmp_path / "public.pem").write_bytes(
        signing_key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    command = (
        [
            "openssl",
            "dgst",
            "-sha256",
            "-verify",
            str(tmp_path / "public.pem"),
            "-signature",
            str(tmp_path / "signature"),
            str(tmp_path / "message"),
        ]
        if isinstance(signing_key, rsa.RSAPrivateKey)
        else [
            "openssl",
            "pkeyutl",
            "-verify",
            "-pubin",
            "-inkey",
            str(tmp_path / "public.pem"),
            "-rawin",
            "-in",
            str(tmp_path / "message"),
            "-sigfile",
            str(tmp_path / "signature"),
        ]
    )
    subprocess.run(command, check=True, capture_output=True)
    api = Api(signing_key.public_key())
    connector = BinanceConnector(connection(), transport=httpx.MockTransport(api.handle), pace=0)
    connector.credentials = credential
    result = asyncio.run(connector.sync(None))
    assert result.complete and result.account_identity == "binance:42"
    assert {path.split("/")[1] for path, _ in api.requests} == {"api", "fapi", "dapi"}
    assert pem(signing_key) not in repr(credential)
    assert "PRIVATE KEY" not in result.model_dump_json()


class StepBroker:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.pending = None
        self.fields = []
        self.results = []

    async def challenge(self, provider, fields, origin):
        assert self.pending is None, "report a step result before the next form"
        assert all(field["kind"] == "secret" for field in fields)
        self.fields.append([field["id"] for field in fields])
        self.pending = str(len(self.fields))
        return self.pending

    async def receive(self):
        values = next(self.replies)
        result = {"type": "answered", "challenge_id": self.pending, "values": values}
        if values is None:
            self.pending = None
        return result

    async def result(self, ident, outcome):
        assert ident == self.pending
        self.pending = None
        self.results.append(outcome)


@pytest.mark.parametrize("source", ["file", "inline", "zond", "missing-file"])
def test_encrypted_keys_and_missing_passphrase_use_zond(signing_key, tmp_path, source):
    encrypted = pem(signing_key, "fixture-passphrase")
    escaped = encrypted.replace("\n", "\\n")
    env_file = tmp_path / ".env.local"
    replies = [{"private_key_passphrase": "fixture-passphrase"}]
    text = f"BINANCE_API_KEY={KEY}\n"
    if source == "file":
        (tmp_path / "key.pem").write_text(encrypted)
        text += "BINANCE_PRIVATE_KEY_FILE=key.pem\n"
    elif source == "inline":
        text += f"BINANCE_PRIVATE_KEY='{escaped}'\n"
    else:
        replies.insert(0, {"private_key": escaped})
        if source == "missing-file":
            text += "BINANCE_PRIVATE_KEY_FILE=absent.pem\n"
    env_file.write_text(text)
    kind = "rsa" if isinstance(signing_key, rsa.RSAPrivateKey) else "ed25519"
    api = Api(signing_key.public_key())
    broker = StepBroker(replies)
    worker = BinanceConnector(
        connection(key_type=kind), broker, transport=httpx.MockTransport(api.handle), pace=0
    )
    assert asyncio.run(worker.sync(None)).complete
    assert broker.fields == (
        [["private_key"], ["private_key_passphrase"]]
        if source in {"zond", "missing-file"}
        else [["private_key_passphrase"]]
    )
    assert broker.results == ["succeeded"] * len(broker.fields)
    assert env_file.read_text() == text
    assert "fixture-passphrase" not in repr(worker.credentials)
    assert all(not reply for reply in replies)


def test_generated_key_is_used_automatically_with_only_api_key_prompt(tmp_path):
    env_file = tmp_path / ".env.local"
    public = generate_key(env_file=env_file)
    api = Api(serialization.load_pem_public_key(public.encode()))
    broker = Broker({"api_key": KEY})
    worker = BinanceConnector(
        connection(), broker, transport=httpx.MockTransport(api.handle), pace=0
    )
    assert asyncio.run(worker.sync(None)).complete
    assert [field["id"] for field in broker.fields] == ["api_key"]
    assert "PRIVATE KEY" not in json.dumps(broker.fields)


def test_auto_mode_accepts_pem_when_env_is_absent(tmp_path):
    private = ed25519.Ed25519PrivateKey.generate()
    api = Api(private.public_key())
    broker = Broker({"api_key": KEY, "api_secret": pem(private).replace("\n", "\\n")})
    worker = BinanceConnector(
        connection(), broker, transport=httpx.MockTransport(api.handle), pace=0
    )
    assert asyncio.run(worker.sync(None)).complete
    assert not (tmp_path / ".env.local").exists()


def test_explicit_hmac_ignores_unused_private_key_file_and_passphrase(tmp_path):
    (tmp_path / ".env.local").write_text(
        f"BINANCE_API_KEY={KEY}\nBINANCE_API_SECRET={SECRET}\n"
        "BINANCE_PRIVATE_KEY_FILE=unreadable\n"
        "BINANCE_PRIVATE_KEY='-----BEGIN ENCRYPTED PRIVATE KEY-----\\nnot-used'\n"
    )
    (tmp_path / "unreadable").mkdir()
    broker = Broker(None)
    worker = BinanceConnector(
        connection(key_type="hmac"), broker, transport=httpx.MockTransport(Api().handle), pace=0
    )
    assert asyncio.run(worker.sync(None)).complete
    assert broker.challenges == 0


def test_invalid_key_types_passphrases_and_files_fail_without_echoing_secrets(tmp_path):
    private = ed25519.Ed25519PrivateKey.generate()
    encrypted = pem(private, "fixture-private-passphrase")
    for material, password, kind in [
        ("private-invalid-material", None, "auto"),
        (encrypted, "wrong-private-passphrase", "auto"),
        (pem(private), None, "rsa"),
        (pem(rsa.generate_private_key(public_exponent=65537, key_size=1024)), None, "rsa"),
        (
            private.public_key()
            .public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
            .decode(),
            None,
            "auto",
        ),
    ]:
        with pytest.raises(LoginRequired) as error:
            Credentials.from_values(
                {"api_key": KEY, "private_key": material, "private_key_passphrase": password}, kind
            )
        assert material not in str(error.value)
        assert "private-passphrase" not in str(error.value)
    env_file = tmp_path / ".env.local"
    env_file.write_text("BINANCE_PRIVATE_KEY_FILE=oversize.pem\n")
    (tmp_path / "oversize.pem").write_bytes(b"x" * 16385)
    with pytest.raises(CredentialFileError, match="exceeds"):
        read_binance_credentials(env_file)
    assert isinstance(
        load_private_key(encrypted, "fixture-private-passphrase"), ed25519.Ed25519PrivateKey
    )
