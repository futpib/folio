#!/usr/bin/env python3
"""Test-only Gateway authentication/TWS peer, isolated from all external networks."""

import json
import os
import runpy
import shutil
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time
from contextlib import suppress
from pathlib import Path

HERE = Path(__file__).resolve().parent
API_PORT = 14002
HTTP_PORT = 18080


def build_patch():
    classes = HERE.parent / "build/patch"
    shutil.rmtree(classes, ignore_errors=True)
    classes.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "javac",
            "--release",
            "17",
            "-cp",
            "/usr/share/ib-tws/jars/*",
            "-d",
            str(classes),
            str(HERE / "AuthenticationPatch.java"),
            str(HERE / "GatewaySession.java"),
        ],
        check=True,
    )
    manifest = HERE.parent / "build/patch.mf"
    manifest.write_text("Premain-Class: AuthenticationPatch\n")
    subprocess.run(
        [
            "jar",
            "--create",
            "--file",
            str(HERE.parent / "build/authentication-patch.jar"),
            "--manifest",
            str(manifest),
            "-C",
            str(classes),
            ".",
        ],
        check=True,
    )


def frame(*fields):
    data = ("\0".join(map(str, fields)) + "\0").encode()
    return struct.pack("!I", len(data)) + data


def exact(client, size):
    result = b""
    while len(result) < size:
        data = client.recv(size - len(result))
        if not data:
            raise EOFError()
        result += data
    return result


def message(client):
    size = struct.unpack("!I", exact(client, 4))[0]
    if size > 65536:
        raise ValueError("oversize test message")
    return exact(client, size)


class Api(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class Peer(socketserver.BaseRequestHandler):
    def handle(self):
        client = self.request
        client.settimeout(1)
        try:
            assert exact(client, 4) == b"API\0"
            assert message(client).startswith(b"v100..")
            client.sendall(frame(178, "20261011 00:00:00 UTC"))
            assert message(client).split(b"\0")[0] == b"71"
            client.sendall(frame(9, 1, 100) + frame(15, 1, "DU424242"))
            while (self.server.base / "authenticated").exists():
                with suppress(TimeoutError):
                    message(client)
        except (OSError, EOFError):
            pass


def api_loop(base):
    while True:
        if (base / "authenticated").exists():
            with Api(("127.0.0.1", API_PORT), Peer) as server:
                server.base = base
                server.timeout = 0.1
                while (base / "authenticated").exists():
                    server.handle_request()
        time.sleep(0.05)


class Control(socketserver.StreamRequestHandler):
    def handle(self):
        action = self.rfile.readline(128).decode().strip()
        base = self.server.base
        if action == "state":
            result = {
                name: (base / name).exists()
                for name in ["patched", "submitted", "authenticated", "patch-failed"]
            }
        elif action in ["accept-mfa", "reauth"]:
            (base / action).touch()
            result = {"ok": True}
        elif action in ["status", "accounts"]:
            command = subprocess.run(
                [str(self.server.repo / ".venv/bin/ibkr-tws"), "--port", str(HTTP_PORT), action],
                capture_output=True,
                text=True,
                timeout=10,
            )
            result = {
                "code": command.returncode,
                "stdout": command.stdout,
                "stderr": command.stderr,
            }
        else:
            result = {"error": "unknown test action"}
        self.wfile.write((json.dumps(result) + "\n").encode())


def main():
    mode, directory, *rest = sys.argv[1:]
    base = Path(directory)
    if mode == "client":
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(15)
            client.connect(str(base / "control.sock"))
            client.sendall((rest[0] + "\n").encode())
            print(client.makefile().readline().strip())
        return
    if mode == "adapter":
        base.mkdir(parents=True, exist_ok=True)
        original = os.readlink("/proc/self/ns/net")
        os.execv(
            "/usr/bin/unshare",
            [
                "unshare",
                "-Urn",
                sys.executable,
                str(HERE / "monkeypatch.py"),
                "isolated",
                str(base),
                rest[0],
                original,
            ],
        )
    if mode == "launch":
        original_exec = os.execv

        def patched_exec(executable, command):
            command = [
                f"-Dfolio.credentials.file={base / '.env.local'}"
                if arg.startswith("-Dfolio.credentials.file=")
                else arg
                for arg in command
            ]
            index = next(i for i, arg in enumerate(command) if arg.startswith("-javaagent:"))
            command.insert(index, f"-javaagent:{HERE.parent}/build/authentication-patch.jar={base}")
            original_exec(executable, command)

        os.execv = patched_exec
        sys.argv = [
            str(HERE.parent / "launch.py"),
            "--settings-dir",
            str(base / "settings"),
            "--mode",
            "paper",
            "--api-port",
            str(API_PORT),
        ]
        runpy.run_path(sys.argv[0], run_name="__main__")
        return
    assert mode == "isolated"
    repo = Path(rest[0])
    assert os.readlink("/proc/self/ns/net") != rest[1], (
        "Gateway must have its own network namespace"
    )
    subprocess.run(["ip", "link", "set", "lo", "up"], check=True)
    links = json.loads(subprocess.check_output(["ip", "-j", "link", "show"]))
    assert [link["ifname"] for link in links] == ["lo"], "only loopback may exist in test namespace"
    for name in [
        "authenticated",
        "submitted",
        "patched",
        "patch-failed",
        "reauth",
        "accept-mfa",
        "control.sock",
        "api-settings",
        "hide",
    ]:
        (base / name).unlink(missing_ok=True)
    config = base / "http.toml"
    config.write_text(f"""[server]
port = {HTTP_PORT}
[tws]
port = {API_PORT}
connect_timeout_seconds = 0.5
request_timeout_seconds = 1
reconnect_delay_seconds = 0.2
[exposure]
expose_account_ids = true
""")
    with (base / "http.log").open("w") as log:
        http = subprocess.Popen(
            [str(repo / ".venv/bin/ibkr-tws-http"), "--config", str(config)],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
        )
        threading.Thread(target=api_loop, args=(base,), daemon=True).start()
        with socketserver.ThreadingUnixStreamServer(str(base / "control.sock"), Control) as control:
            control.base, control.repo = base, repo
            threading.Thread(target=control.serve_forever, daemon=True).start()
            try:
                subprocess.run(
                    [sys.executable, str(HERE / "monkeypatch.py"), "launch", str(base)], check=True
                )
            finally:
                http.terminate()
                http.wait(timeout=10)


if __name__ == "__main__":
    main()
