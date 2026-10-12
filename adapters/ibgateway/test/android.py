#!/usr/bin/env python3
"""Exercise Gateway authentication through the real zond Android app and HTTP service."""

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path

from monkeypatch import build_patch

ADAPTER = Path(__file__).resolve().parents[1]
REPO = ADAPTER.parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zondd-repo", type=Path, required=True)
    args = parser.parse_args()
    zondd = args.zondd_repo.resolve()
    subprocess.run(["cargo", "build", "--workspace", "--bins"], cwd=zondd, check=True)
    subprocess.run([str(ADAPTER / "build.sh")], check=True)
    build_patch()
    with tempfile.TemporaryDirectory(prefix="ibgateway-android-") as directory:
        base = Path(directory)
        fixture = base / "fixture.json"
        fixture.write_text(
            json.dumps(
                {
                    "gateway": {
                        "repo": str(REPO),
                        "script": str(ADAPTER / "test/monkeypatch.py"),
                        "directory": str(base / "gateway"),
                    },
                    "forbiddenLogValues": ["test-user", "test-gateway-password"],
                    "additionalLogFiles": [
                        str(base / "gateway" / transport / "http.log")
                        for transport in ["ssh", "iroh"]
                    ],
                }
            )
        )
        environment = os.environ.copy()
        environment["ZONDD_ANDROID_EXTRA_FIXTURE"] = str(fixture)
        environment["ZONDD_ANDROID_TEST"] = str(ADAPTER / "test/integration_test/gateway_test.dart")
        subprocess.run(
            [
                "cargo",
                "test",
                "-p",
                "zondctl",
                "--test",
                "android",
                "--",
                "--ignored",
                "--nocapture",
            ],
            cwd=zondd,
            env=environment,
            check=True,
        )


if __name__ == "__main__":
    main()
