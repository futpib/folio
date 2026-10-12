"""Binance signing keys and local key generation. Never print private material."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from .credentials import CredentialFileError, credential_path, read_credentials
from .models import LoginRequired
from .store import exclusive


class PassphraseRequired(LoginRequired):
    pass


def private_pem(value: str) -> bytes:
    # Zond answer fields are single-line; only PEM material decodes literal newlines.
    return value.replace("\\n", "\n").strip().encode("utf-8")


def load_private_key(value: str, passphrase: str | None = None):
    pem = private_pem(value)
    encrypted = pem.startswith(b"-----BEGIN ENCRYPTED PRIVATE KEY-----")
    if not encrypted and not pem.startswith(b"-----BEGIN PRIVATE KEY-----"):
        raise LoginRequired("Use a PKCS#8 PEM private key for Binance, not a public or SSH key.")
    if encrypted and not passphrase:
        raise PassphraseRequired("The Binance private key needs its passphrase.")
    try:
        key = serialization.load_pem_private_key(
            pem, password=passphrase.encode("utf-8") if encrypted else None
        )
    except (ValueError, TypeError, UnsupportedAlgorithm):
        raise LoginRequired(
            "Cannot unlock the Binance private key; check its PEM and passphrase."
        ) from None
    if isinstance(key, rsa.RSAPrivateKey):
        if key.key_size not in (2048, 4096):
            raise LoginRequired("Binance RSA keys must be 2048 or 4096 bits.")
    elif not isinstance(key, ed25519.Ed25519PrivateKey):
        raise LoginRequired("Binance self-generated keys must use Ed25519 or RSA.")
    return key


def read_binance_credentials(path=None, *, key_type="auto"):
    values = read_credentials("binance", path)
    if key_type == "hmac":
        return values
    if values.get("private_key_file") and not values.get("private_key"):
        target = Path(values["private_key_file"]).expanduser()
        if not target.is_absolute():
            target = credential_path(path).parent / target
        try:
            with target.open(encoding="utf-8") as stream:
                pem = stream.read(16385)
        except FileNotFoundError:
            return values
        except (OSError, UnicodeError):
            raise CredentialFileError("Cannot read the Binance private key file.") from None
        if len(pem) > 16384:
            raise CredentialFileError("Binance private key file exceeds 16 KiB.")
        if pem.strip():
            values["private_key"] = pem
    return values


@dataclass(repr=False)
class Credentials:
    key: str = field(repr=False)
    secret: str = field(default="", repr=False)
    private_key: ed25519.Ed25519PrivateKey | rsa.RSAPrivateKey | None = field(
        default=None, repr=False
    )

    @classmethod
    def from_values(cls, values, key_type="auto"):
        key = values.get("api_key")
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9]{16,512}", key):
            raise LoginRequired("Set a valid Binance API key in .env.local or Zond.")
        pem = values.get("private_key")
        secret = values.get("api_secret", "")
        if (
            not pem
            and key_type == "auto"
            and isinstance(secret, str)
            and secret.lstrip().startswith("-----BEGIN")
        ):
            pem = secret
        if pem and key_type != "hmac":
            private = load_private_key(pem, values.get("private_key_passphrase"))
            actual = "ed25519" if isinstance(private, ed25519.Ed25519PrivateKey) else "rsa"
            if key_type not in ("auto", actual):
                raise LoginRequired("Binance private key does not match binance.key_type.")
            return cls(key, private_key=private)
        if key_type in ("ed25519", "rsa"):
            raise LoginRequired("Set the Binance private key in .env.local or Zond.")
        if not isinstance(secret, str) or not re.fullmatch(r"[A-Za-z0-9]{16,512}", secret):
            raise LoginRequired("Set a valid Binance signing key in .env.local or Zond.")
        return cls(key, secret)

    def sign(self, payload: str) -> str:
        data = payload.encode("utf-8")
        if isinstance(self.private_key, rsa.RSAPrivateKey):
            signature = self.private_key.sign(data, padding.PKCS1v15(), hashes.SHA256())
        elif isinstance(self.private_key, ed25519.Ed25519PrivateKey):
            signature = self.private_key.sign(data)
        else:
            return hmac.new(self.secret.encode(), data, hashlib.sha256).hexdigest()
        return base64.b64encode(signature).decode("ascii")


def generate_key(key_type="ed25519", *, env_file=None) -> str:
    """Create once, register the local path, and return only the public key PEM."""
    env_file = credential_path(env_file)
    directory = env_file.parent / ".secrets"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = directory / f"binance-{key_type}.pem"
    with exclusive(directory / "binance-keygen.lock"):
        existing = read_credentials("binance", env_file)
        registered = existing.get("private_key_file")
        if existing.get("private_key") or (registered and registered != f".secrets/{target.name}"):
            raise CredentialFileError(
                "A different Binance private key is already configured; it was not replaced."
            )
        existing.clear()
        if target.exists():
            try:
                private = load_private_key(target.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, LoginRequired):
                raise CredentialFileError(
                    "Existing Binance key cannot be read; it was not replaced."
                ) from None
            if (isinstance(private, ed25519.Ed25519PrivateKey)) != (key_type == "ed25519"):
                raise CredentialFileError(
                    "Existing Binance key has the wrong type; it was not replaced."
                )
        else:
            private = (
                ed25519.Ed25519PrivateKey.generate()
                if key_type == "ed25519"
                else rsa.generate_private_key(public_exponent=65537, key_size=2048)
            )
            pem = private.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            with target.open("xb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(pem)
                stream.flush()
                os.fsync(stream.fileno())
        # Preserve other credentials and comments; only register the generated signing key.
        try:
            content = env_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            content = ""
        names = r"BINANCE_PRIVATE_KEY(?:_FILE)?"
        lines = [
            line
            for line in content.splitlines()
            if not re.match(rf"\s*(?:export\s+)?{names}\s*(?:=|$)", line)
        ]
        lines.append(f"BINANCE_PRIVATE_KEY_FILE=.secrets/{target.name}")
        fd, temporary = tempfile.mkstemp(prefix=".env.local-", dir=env_file.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write("\n".join(lines) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, env_file)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return (
            private.public_key()
            .public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
            .decode("ascii")
        )
