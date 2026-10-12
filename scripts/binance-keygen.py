#!/usr/bin/env python3
"""Generate a Binance signing key in this checkout and print its public key."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    uv = shutil.which("uv")
    if uv is None:
        sys.exit("Install uv, then rerun this script on the machine running Folio.")
    os.umask(0o077)
    os.execve(
        uv,
        [
            uv,
            "run",
            "--project",
            str(ROOT),
            "--locked",
            "--no-dev",
            "--no-env-file",
            "folio",
            "binance-keygen",
            *sys.argv[1:],
        ],
        {**os.environ, "UV_PROJECT_ENVIRONMENT": str(ROOT / ".venv")},
    )


if __name__ == "__main__":
    main()
