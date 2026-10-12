#!/usr/bin/env python3
"""Gateway adapter E2E with a fixture or the installed application."""

import argparse
import json
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from monkeypatch import API_PORT, build_patch

ADAPTER = Path(__file__).resolve().parents[1]


def wait_for(read, predicate, seconds=45):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = read()
        if predicate(value):
            return value
        time.sleep(0.1)
    raise AssertionError("timed out waiting for expected application state")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--real", action="store_true", help="observe installed Gateway login only")
    mode.add_argument(
        "--stub-auth",
        action="store_true",
        help="installed Gateway with offline authentication and its real settings UI",
    )
    parser.add_argument("--zondd", type=Path, required=True, help="zondd daemon executable")
    args = parser.parse_args()
    subprocess.run([str(ADAPTER / "build.sh")], check=True)
    if args.stub_auth:
        build_patch()
    subprocess.run(
        [
            "javac",
            "--release",
            "17",
            "-d",
            str(ADAPTER / "build/test"),
            str(ADAPTER / "test/GatewayFixture.java"),
            str(ADAPTER / "test/GatewaySettingsFixture.java"),
            str(ADAPTER / "test/ApiConfigurationTest.java"),
        ],
        check=True,
    )
    with tempfile.TemporaryDirectory(prefix="zond-gateway-e2e-") as temp:
        base = Path(temp)
        subprocess.run(
            [
                "xvfb-run",
                "-a",
                "java",
                "-cp",
                f"{ADAPTER}/build/test:{ADAPTER}/build/zond-ibgateway.jar",
                "ApiConfigurationTest",
                str(base),
            ],
            check=True,
            timeout=30,
        )
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        if args.stub_auth:
            port = API_PORT
            program = sys.executable
            argv = [
                str(ADAPTER / "test/monkeypatch.py"),
                "adapter",
                str(base),
                str(ADAPTER.parents[1]),
            ]
        elif args.real:
            program = str(ADAPTER / "launch.py")
            argv = [
                "--settings-dir",
                str(base / "settings"),
                "--mode",
                "paper",
                "--api-port",
                str(port),
            ]
        else:
            program = "/usr/bin/xvfb-run"
            argv = [
                "-a",
                "-e",
                "/dev/null",
                "/usr/bin/java",
                f"-javaagent:{ADAPTER}/build/zond-ibgateway.jar=paper:{port}",
                "-cp",
                f"{ADAPTER}/build/test:/usr/share/ib-tws/jars/*",
                "GatewayFixture",
                str(port),
                str(base),
            ]
        config = base / "config.toml"
        config.write_text(f'''runtime_dir = "{base}/run"
[credential_adapters.gateway]
label = "Configured test Gateway"
program = {json.dumps(program)}
args = {json.dumps(argv)}
cwd = {json.dumps(str(base))}
''')
        config.chmod(0o600)
        with (base / "daemon.log").open("w") as log:
            daemon = subprocess.Popen(
                [str(args.zondd.resolve()), "serve", "--config", str(config)],
                stdout=log,
                stderr=log,
            )

            def request(body):
                with socket.socket(socket.AF_UNIX) as client:
                    client.settimeout(5)
                    client.connect(str(base / "run/admin.sock"))
                    client.sendall(
                        (json.dumps({"version": 1, "id": 1, "body": body}) + "\n").encode()
                    )
                    return json.loads(client.makefile("rb").readline())["body"]

            def pending():
                if (base / "patch-failed").exists():
                    raise AssertionError((base / "patch-failed").read_text())
                return request({"method": "pending"})

            def control(action):
                with socket.socket(socket.AF_UNIX) as client:
                    client.settimeout(15)
                    client.connect(str(base / "control.sock"))
                    client.sendall((action + "\n").encode())
                    return json.loads(client.makefile("rb").readline())

            def challenge():
                return wait_for(pending, lambda q: bool(q["interactions"]))["interactions"][0]

            try:
                wait_for(lambda: (base / "run/admin.sock").exists(), bool)
                first = challenge()
                assert first["recipient"]["label"] == "Configured test Gateway"
                assert first["context"]["mode"] == "paper"
                assert first["context"]["read_only_api"] is True
                assert [f["id"] for f in first["fields"]] == ["username", "password"]
                request(
                    {
                        "method": "answer_interaction",
                        "challenge_id": first["challenge_id"],
                        "values": None,
                    }
                )
                time.sleep(1)
                assert not pending()["interactions"], "declined form must not repeatedly prompt"
                request({"method": "retry_adapter", "adapter": "gateway"})
                second = challenge()
                assert second["challenge_id"] != first["challenge_id"]
                if args.real:
                    request(
                        {
                            "method": "answer_interaction",
                            "challenge_id": second["challenge_id"],
                            "values": None,
                        }
                    )
                    print(
                        "PASS: installed Gateway login detected; decline and retry; "
                        "no credentials submitted"
                    )
                    return
                for current in [second, None]:
                    current = current or challenge()
                    if args.stub_auth:
                        wait_for(
                            lambda: control("status"),
                            lambda r: (
                                r["code"] == 0 and not json.loads(r["stdout"])["tws_connected"]
                            ),
                        )
                    response = request(
                        {
                            "method": "answer_interaction",
                            "challenge_id": current["challenge_id"],
                            "values": {
                                "username": "test-user",
                                "password": "test-gateway-password",
                            },
                        }
                    )
                    assert response["type"] == "credential_answered"
                    if args.stub_auth:
                        wait_for(lambda: (base / "submitted").exists(), bool)
                        assert not (base / "authenticated").exists()
                        assert pending()["adapters"][0]["state"] != "ready"
                        control("accept-mfa")
                    wait_for(pending, lambda q: q["adapters"][0]["state"] == "ready")
                    assert (base / "submitted").read_text() == "yes"
                    assert (base / "api-settings").read_text() == f"{port} true true"
                    if args.stub_auth:
                        accounts = wait_for(lambda: control("accounts"), lambda r: r["code"] == 0)
                        assert "DU424242" in accounts["stdout"]
                    (base / "reauth").touch()
                challenge()
                (base / "hide").touch()
                wait_for(pending, lambda q: not q["interactions"])
                audit = json.dumps(
                    request({"method": "actions", "limit": 100, "query": "interaction"})
                )
                assert "withdrawn" in audit and "succeeded" in audit and "rejected" in audit
                assert "test-gateway-password" not in audit and "test-user" not in audit
                for path in base.glob("config.history.sqlite3*"):
                    assert b"test-gateway-password" not in path.read_bytes()
                assert "test-gateway-password" not in (base / "daemon.log").read_text()
                if args.stub_auth:
                    assert "test-gateway-password" not in (base / "http.log").read_text()
                print(
                    f"PASS: {'installed Gateway' if args.stub_auth else 'Swing fixture'} login, "
                    "private delivery, saved read-only API settings, "
                    "API handshake, reauthentication, "
                    "decline/retry, withdrawal, audit"
                )
            except Exception:
                print((base / "daemon.log").read_text()[-6000:])
                raise
            finally:
                daemon.terminate()
                daemon.wait(timeout=15)


if __name__ == "__main__":
    main()
