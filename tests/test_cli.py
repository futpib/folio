from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import uvicorn

from ibkr_tws_http.app import create_app
from ibkr_tws_http.cli import main
from ibkr_tws_http.config import ServerConfig, Settings, TwsConfig
from ibkr_tws_http.mock_tws import create_mock_gateway


@pytest.fixture
def http_service():
    requests: list[tuple[str, str, bytes]] = []
    reply = {"status": 200, "body": b'{"price":1.234567890123456789,"complete":false}'}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            requests.append((self.command, self.path, body))
            self.send_response(reply["status"])
            self.send_header("Location", "/must-not-follow")
            self.end_headers()
            self.wfile.write(reply["body"])

        do_POST = do_GET

        def log_message(self, *_args) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_port, requests, reply
        finally:
            server.shutdown()
            thread.join(timeout=5)


def test_json_is_preserved_and_proxy_environment_is_ignored(http_service, capsys, monkeypatch):
    port, requests, _ = http_service
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setenv("NO_PROXY", "")
    assert main(["--port", str(port), "positions"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == '{"price":1.234567890123456789,"complete":false}\n'
    assert requests == [("GET", "/v1/positions", b"")]


def test_post_contract_and_history_options_reach_service(http_service, capsys):
    port, requests, _ = http_service
    assert (
        main(
            [
                "historical-bars",
                "--port",
                str(port),
                "--symbol",
                "spy",
                "--sec-type",
                "stk",
                "--duration",
                "2 D",
                "--bar-size",
                "1 hour",
                "--outside-rth",
                "--end",
                "2026-10-10T12:00:00Z",
            ]
        )
        == 0
    )
    method, path, body = requests[0]
    assert (method, path) == ("POST", "/v1/historical-bars")
    payload = json.loads(body)
    assert payload["contract"] == {
        "symbol": "SPY",
        "sec_type": "STK",
        "exchange": "SMART",
        "currency": "USD",
    }
    assert payload["regular_trading_hours_only"] is False
    assert payload["duration"] == "2 D"
    assert payload["end"] == "2026-10-10T12:00:00Z"
    assert capsys.readouterr().err == ""


def test_search_cannot_inject_query_parameters(http_service):
    port, requests, _ = http_service
    assert main(["contract-search", "A&B?pattern=OTHER", "--port", str(port)]) == 0
    query = parse_qs(urlsplit(requests[0][1]).query)
    assert query == {"pattern": ["A&B?pattern=OTHER"]}


@pytest.mark.parametrize(
    "status,body,detail",
    [
        (502, b'{"tws_error":{"code":162,"message":"not subscribed"}}', '"code":162'),
        (422, b'{"detail":[{"msg":"invalid query"}]}', "invalid query"),
        (302, b"{}", "HTTP 302"),
        (200, b"<html>wrong server</html>", "valid JSON"),
    ],
)
def test_failures_are_nonzero_with_empty_stdout(http_service, capsys, status, body, detail):
    port, requests, reply = http_service
    reply.update(status=status, body=body)
    assert main(["--port", str(port), "status"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert detail in captured.err
    assert len(requests) == 1


def test_oversized_response_is_not_printed(http_service, capsys, monkeypatch):
    port, _, _ = http_service
    monkeypatch.setattr("ibkr_tws_http.cli.MAX_RESPONSE_BYTES", 8)
    assert main(["--port", str(port), "status"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "limit" in captured.err


def test_closed_output_pipe_has_no_shutdown_traceback(http_service):
    port, _, _ = http_service
    executable = Path(sys.executable).with_name("ibkr-tws")
    with subprocess.Popen(
        [str(executable), "--port", str(port), "positions"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as process:
        process.stdout.close()
        assert process.wait(timeout=10) == 1
        assert process.stderr.read() == b""


@pytest.mark.parametrize(
    "arguments",
    [
        ["--host", "example.com", "status"],
        ["--host", "192.168.1.1", "status"],
        ["--port", "0", "status"],
        ["--timeout", "nan", "status"],
        ["--timeout", "inf", "status"],
        ["--timeout", "1e300", "status"],
        ["--timeout", "-1", "status"],
        ["snapshot"],
        ["snapshot", "--symbol", "AAPL", "--strike", "nan"],
        ["historical-bars", "--symbol", "AAPL", "--end", "2026-10-10T12:00:00"],
        ["chain-snapshot", "AAPL", "20260918", "--strike-min", "20", "--strike-max", "10"],
        ["chain-snapshot", "AAPL", "20260918", "--max-contracts", "41"],
        ["observed-history", "--symbol", "AAPL"],
        [
            "volatility-comparison",
            "--option-contract",
            '{"symbol":"AAPL"}',
            "--underlying-contract",
            '{"symbol":"AAPL"}',
        ],
        ["contract-search", ""],
    ],
)
def test_invalid_input_fails_before_network_request(arguments, capsys):
    with pytest.raises(SystemExit) as exc:
        main(arguments)
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "error:" in captured.err


def test_connection_refusal_has_actionable_error(capsys):
    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        assert main(["--port", str(unused.getsockname()[1]), "health"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Check that ibkr-tws-http is running" in captured.err


@pytest.fixture(scope="module")
def mock_service() -> Iterator[int]:
    tws = TwsConfig(
        connect_timeout_seconds=0.1, request_timeout_seconds=0.1, reconnect_delay_seconds=0.1
    )
    app = create_app(Settings(server=ServerConfig(), tws=tws), gateway=create_mock_gateway(tws))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started, "mock HTTP service failed to start"
            yield listener.getsockname()[1]
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive(), "mock HTTP service failed to stop"


@pytest.mark.parametrize(
    "arguments,expected_code",
    [
        (["health"], 0),
        (["status"], 0),
        (["capabilities"], 0),
        (["accounts"], 0),
        (["account-summary"], 0),
        (["positions"], 0),
        (["contract-search", "XXI"], 0),
        (["contract-details", "--symbol", "XXI"], 0),
        (["option-chains", "XXI", "837468789"], 0),
        (
            [
                "chain-snapshot",
                "XXI",
                "20280121",
                "--max-contracts",
                "2",
                "--timeout-seconds",
                "2",
                "--strike-min",
                "2.5",
                "--strike-max",
                "2.5",
            ],
            0,
        ),
        (["portfolio-snapshot", "--timeout-seconds", "2"], 0),
        (
            [
                "snapshot",
                "--con-id",
                "829400001",
                "--sec-type",
                "OPT",
                "--strike",
                "2.5",
                "--right",
                "C",
            ],
            0,
        ),
        (["historical-bars", "--con-id", "829400001", "--sec-type", "OPT"], 1),
        (["volatility-history", "--symbol", "XXI"], 0),
        (
            [
                "volatility-comparison",
                "--option-contract",
                '{"con_id":829400001,"sec_type":"OPT","strike":2.5,"right":"C"}',
                "--underlying-contract",
                '{"symbol":"XXI"}',
            ],
            0,
        ),
        (["observed-history", "--con-id", "829400001", "--sec-type", "OPT"], 0),
    ],
)
def test_installed_cli_against_mock_tws_callbacks(mock_service, arguments, expected_code):
    executable = Path(sys.executable).with_name("ibkr-tws")
    result = subprocess.run(
        [str(executable), "--port", str(mock_service), *arguments],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
        env={"PATH": "/usr/bin:/bin"},
        cwd="/",
    )
    assert result.returncode == expected_code, result.stderr
    if expected_code == 0:
        assert result.stderr == ""
        assert isinstance(json.loads(result.stdout), (dict, list))
    else:
        assert result.stdout == ""
        assert '"code":2188' in result.stderr
