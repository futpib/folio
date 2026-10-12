from __future__ import annotations

import json
import shutil
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

import folio.browser as module
from folio.browser import BrowserConnector
from folio.config import Connection, Settings
from folio.providers import BOG_ACCOUNTS, PAGES, WOLT_ORDERS
from folio.service import create_app

pytestmark = pytest.mark.skipif(not shutil.which("chromium"), reason="requires installed Chromium")


@pytest.mark.parametrize("provider", ["bog", "wolt"])
def test_real_browser_call_reuses_env_login_and_headers_without_pagination(
    tmp_path, monkeypatch, provider
):
    env = tmp_path / ".env.local"
    env.write_text(
        "BOG_USERNAME=fixture-user\nBOG_PASSWORD=fixture-password\nWOLT_EMAIL=fixture@example.com\n"
    )
    selected = Connection(provider, provider)
    settings = Settings(tmp_path, (selected,), chromium=shutil.which("chromium"))
    connector = BrowserConnector(settings, selected, env_file=env)
    original = module.browser
    requests = []
    api = f"/rest/rb?serviceId={BOG_ACCOUNTS}" if provider == "bog" else WOLT_ORDERS
    login_fields = (
        '<input name="username"><input type="password">'
        if provider == "bog"
        else '<input type="email">'
    )
    validation = (
        "if(this.elements.username.value !== 'fixture-user' || "
        "this.querySelector('[type=password]').value !== 'fixture-password') return;"
        if provider == "bog"
        else "if(this.querySelector('input').value !== 'fixture@example.com') return;"
    )

    async def route(request):
        path = urlsplit(request.request.url).path
        requests.append((request.request.method, path))
        if path == urlsplit(PAGES[provider][0]).path:
            await request.fulfill(
                content_type="text/html",
                body=f"""
                <form onsubmit="event.preventDefault();{validation}
                  this.remove();fetch('{api}', {{headers:
                    {{'authorization':'Bearer fixture-session-token'}}}})">
                {login_fields}<button type="submit">Continue</button></form>
                <script>fetch('{api}')</script>""",
            )
        elif path == urlsplit(api).path:
            if request.request.headers.get("authorization") == "Bearer fixture-session-token":
                await request.fulfill(json={"orders": []} if provider == "wolt" else {"code": 0})
            else:
                await request.fulfill(status=401, json={"error": "expired"})
        elif path == "/smoke/new-read":
            assert request.request.headers["authorization"] == "Bearer fixture-session-token"
            await request.fulfill(
                content_type="application/json",
                body='{"amount":123.4567890123456789012345,"echo":"fixture-session-token","next":"another-page"}',
            )
        elif path == "/smoke/denied":
            await request.fulfill(status=403, json={"error": "Fixture permission denied"})
        elif path == "/smoke/redirect":
            await request.fulfill(status=302, headers={"Location": "https://attacker.test/steal"})
        else:
            await request.abort()

    @asynccontextmanager
    async def browser(*args, **kwargs):
        async with original(*args, **kwargs) as context:
            await context.route("**/*", route)
            yield context

    monkeypatch.setattr(module, "browser", browser)
    module_app = create_app(settings, connectors={provider: connector})
    with TestClient(module_app) as client:
        response = client.post(
            f"/v1/connections/{provider}/call", json={"endpoint": "/smoke/new-read"}
        )
        result = response.json()
        assert response.status_code == 200, result
        assert result["status"] == 200
        assert (
            result["body"]
            == '{"amount":123.4567890123456789012345,"echo":"<redacted>","next":"another-page"}'
        )
        assert requests.count(("GET", "/smoke/new-read")) == 1
        assert all(method == "GET" for method, _ in requests)
        assert all(row["fields"]["call_id"] == result["id"] for row in result["logs"])
        assert "fixture-session-token" not in json.dumps(result["logs"])
        with module_app.state.store.db() as db:
            assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
        denied = client.post(
            f"/v1/connections/{provider}/call", json={"endpoint": "/smoke/denied"}
        ).json()
        assert denied["status"] == 403
        assert json.loads(denied["body"])["error"] == "Fixture permission denied"
        assert requests.count(("GET", "/smoke/denied")) == 1
        redirect = client.post(
            f"/v1/connections/{provider}/call", json={"endpoint": "/smoke/redirect"}
        )
        assert redirect.status_code == 502 and "redirected" in redirect.json()["error"]
        assert not any(path == "/steal" for _, path in requests)
