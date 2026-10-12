"""Read local credentials without exporting them to the process environment."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

DEFAULT_ENV_FILE = Path(__file__).resolve().parents[2] / ".env.local"
FIELDS = {
    "binance": (
        "api_key",
        "api_secret",
        "private_key",
        "private_key_file",
        "private_key_passphrase",
    ),
    "bog": ("username", "password"),
    "wolt": ("username", "email", "password"),
    "ibkr": ("username", "password"),
}


class CredentialFileError(Exception):
    pass


def credential_path(path: Path | None = None) -> Path:
    return path if path is not None else DEFAULT_ENV_FILE


def read_credentials(provider: str, path: Path | None = None) -> dict[str, str]:
    names = {f"{provider}_{field}".upper(): field for field in FIELDS[provider]}
    try:
        with credential_path(path).open(encoding="utf-8") as stream:
            content = stream.read(65537)
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError):
        raise CredentialFileError(
            "Cannot read .env.local; check its encoding and permissions."
        ) from None
    if len(content) > 65536:
        raise CredentialFileError(".env.local exceeds 64 KiB.")
    values = {}
    for line in content.splitlines():
        match = re.fullmatch(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z_0-9]*)\s*(?:=(.*))?", line)
        if not match or match[1] not in names:
            continue
        raw = (match[2] or "").strip()
        if raw.startswith(("'", '"')):
            quote = raw[0]
            quoted = re.fullmatch(rf"{quote}((?:[^\\{quote}]|\\.)*){quote}\s*(?:#.*)?", raw)
            if not quoted:
                raise CredentialFileError(
                    "Invalid quoted value in .env.local; use one line per entry."
                )
            value = re.sub(rf"\\([\\{quote}])", r"\1", quoted[1])
        else:
            value = re.sub(r"(?:^|\s+)#.*$", "", raw).rstrip()
        if "\x00" in value or len(value) > 4096:
            raise CredentialFileError("Invalid credential length or NUL character in .env.local.")
        # Empty entries deliberately fall back to Zond. Never expand $variables or shell code.
        field = names[match[1]]
        if value:
            values[field] = value
        else:
            values.pop(field, None)
    return values


if __name__ == "__main__":
    # The Gateway Java agent reads this pipe directly; it never goes through a shell or logs.
    try:
        print(json.dumps(read_credentials("ibkr", Path(sys.argv[1]))))
    except CredentialFileError as error:
        sys.exit(str(error))
