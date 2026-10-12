from __future__ import annotations

import asyncio
import json
import shutil
from urllib.parse import parse_qs, urlsplit

import pytest
from test_folio import accounts, order, transaction

from folio.browser import BrowserConnector, authenticate, browser
from folio.config import Connection, Settings
from folio.models import RateLimited
from folio.providers import BOG_ACCOUNTS, WOLT_ORDERS

pytestmark = pytest.mark.skipif(not shutil.which("chromium"), reason="requires installed Chromium")


def test_real_chromium_profile_pagination_and_limits(tmp_path):
    async def run():
        connection = Connection("wolt", "wolt", max_pages=2)
        settings = Settings(tmp_path, (connection,), chromium=shutil.which("chromium"))
        requests = []

        async def routes(route):
            url = urlsplit(route.request.url)
            requests.append((route.request.method, url.path))
            if url.path == "/en/profile/order-history":
                await route.fulfill(
                    content_type="text/html",
                    body=f"""<html><body>History
                <script>localStorage.setItem('session', 'private-fixture-session');
                fetch('{WOLT_ORDERS}');</script></body></html>""",
                )
            elif url.path == WOLT_ORDERS:
                second = bool(parse_qs(url.query).get("page_token"))
                await route.fulfill(
                    json={
                        "orders": [order("two" if second else "one")],
                        "next_page_token": None if second else "page-two",
                    }
                )
            else:
                await route.abort()

        async with browser(settings, connection) as context:
            await context.route("**/*", routes)
            result = await BrowserConnector(settings, connection).collect(context, None)
            assert [r.id for r in result.records] == ["one", "two"]
            assert result.complete
        # A fresh browser process must keep the private account session.
        async with browser(settings, connection) as context:
            await context.route("**/*", routes)
            page = await context.new_page()
            await page.goto("https://wolt.com/en/profile/order-history")
            assert (
                await page.evaluate("localStorage.getItem('session')") == "private-fixture-session"
            )
            with pytest.raises(Exception, match="profile is in use"):
                async with browser(settings, connection):
                    pass
        assert all(method == "GET" for method, _ in requests)
        assert not any("order-again" in path for _, path in requests)

    asyncio.run(run())


@pytest.mark.parametrize("present", [False, True])
def test_wolt_email_uses_file_or_requests_missing_value(tmp_path, present):
    if present:
        (tmp_path / ".env.local").write_text("WOLT_EMAIL=fixture@example.com\n")

    class EmailBroker:
        def __init__(self):
            self.fields = []

        async def challenge(self, provider, fields, page_origin):
            assert provider == "wolt" and page_origin == "https://wolt.com"
            self.fields = fields
            return "email"

        async def receive(self):
            return {
                "type": "answered",
                "challenge_id": "email",
                "values": {"email": "fixture@example.com"},
            }

        async def result(self, ident, result):
            assert result == "succeeded"

    async def run():
        connection = Connection("wolt", "wolt")
        settings = Settings(tmp_path, (connection,), chromium=shutil.which("chromium"))
        async with browser(settings, connection) as context:

            async def route(request):
                await request.fulfill(
                    content_type="text/html",
                    body="""
                    <form onsubmit="event.preventDefault();
                    if(this.querySelector('input').value==='fixture@example.com')this.remove()">
                    <input type="email"><button type="submit">Continue</button></form>""",
                )

            await context.route("**/*", route)
            page = await context.new_page()
            await page.goto("https://wolt.com/login")
            broker = EmailBroker()
            assert await authenticate(page, "wolt", broker)
            assert [f["id"] for f in broker.fields] == ([] if present else ["email"])

    asyncio.run(run())


def test_real_chromium_bog_accounts_and_cursor_pagination(tmp_path):
    async def run():
        connection = Connection("bog", "bog", max_pages=3)
        settings = Settings(tmp_path, (connection,), chromium=shutil.which("chromium"))
        captured_headers = []

        async def route(request):
            url = urlsplit(request.request.url)
            if url.path in {"/accounts", "/operations/"}:
                api = (
                    f"/rest/rb?serviceId={BOG_ACCOUNTS}"
                    if url.path == "/accounts"
                    else "/rest/transactional/statements?limit=1"
                )
                await request.fulfill(
                    content_type="text/html",
                    body=f"""<html><body>
                <script>fetch('{api}', {{headers: {{'x-ext-customer-id':'private-token'}}}});
                </script></body></html>""",
                )
            elif url.path == "/rest/rb":
                await request.fulfill(json=accounts())
            elif url.path == "/rest/transactional/statements":
                captured_headers.append(await request.request.all_headers())
                query = parse_qs(url.query)
                await request.fulfill(
                    json={"data": [] if "searchAndSort" in query else [transaction(1)]}
                )
            else:
                await request.abort()

        async with browser(settings, connection) as context:
            await context.route("**/*", route)
            result = await BrowserConnector(settings, connection).collect(context, None)
            assert len(result.records) == 4
            assert result.complete
            assert [h["x-ext-customer-id"] for h in captured_headers] == ["private-token"] * 2
            assert "private-token" not in result.model_dump_json()

    asyncio.run(run())


def test_real_chromium_rate_limit_and_page_cap(tmp_path):
    async def run():
        connection = Connection("wolt", "wolt", max_pages=1)
        settings = Settings(tmp_path, (connection,), chromium=shutil.which("chromium"))
        limited = False

        async def route(request):
            if urlsplit(request.request.url).path == WOLT_ORDERS:
                if limited:
                    await request.fulfill(status=429, headers={"Retry-After": "900"}, body="{}")
                else:
                    await request.fulfill(json={"orders": [order()], "next_page_token": "more"})
            else:
                await request.fulfill(
                    content_type="text/html", body=f"<script>fetch('{WOLT_ORDERS}')</script>"
                )

        async with browser(settings, connection) as context:
            await context.route("**/*", route)
            connector = BrowserConnector(settings, connection)
            assert not (await connector.collect(context, None)).complete
            limited = True
            with pytest.raises(RateLimited) as error:
                await connector.collect(context, None)
            assert error.value.retry_after == 900

    asyncio.run(run())


class FixtureBroker:
    def __init__(self):
        self.fields = []
        self.results = []

    async def challenge(self, provider, fields, page_origin):
        assert page_origin == "https://account.bog.ge"
        self.fields = fields
        return "challenge"

    async def receive(self):
        return {
            "type": "answered",
            "challenge_id": "challenge",
            "values": {"username": "fixture", "password": "private-answer"},
        }

    async def result(self, ident, result):
        self.results.append(result)


@pytest.mark.parametrize("present", [(), ("username",), ("password",), ("username", "password")])
def test_real_chromium_login_fills_only_trusted_origin(tmp_path, present):
    values = {"username": "local-user", "password": "local-password"}
    (tmp_path / ".env.local").write_text(
        "".join(f"BOG_{name.upper()}={values[name]}\n" for name in present)
    )
    expected = {
        "username": "local-user" if "username" in present else "fixture",
        "password": "local-password" if "password" in present else "private-answer",
    }

    async def run():
        connection = Connection("bog", "bog")
        settings = Settings(tmp_path, (connection,), chromium=shutil.which("chromium"))
        async with browser(settings, connection) as context:

            async def route(request):
                await request.fulfill(
                    content_type="text/html",
                    body="""
                    <form onsubmit="event.preventDefault();window.filled={
                        username:this.querySelector('[name=username]').value,
                        password:this.querySelector('[type=password]').value};this.remove()">
                    <input name="username"><input type="password">
                    <button type="submit">Log in</button></form>""",
                )

            await context.route("**/*", route)
            page = await context.new_page()
            broker = FixtureBroker()
            await page.goto("https://attacker.test/login")
            assert not await authenticate(page, "bog", broker)
            assert not broker.fields
            await page.goto("https://account.bog.ge/login")
            assert await authenticate(page, "bog", broker)
            assert await page.evaluate("window.filled") == expected
            assert [f["id"] for f in broker.fields] == [
                name for name in values if name not in present
            ]
            assert broker.results == (["succeeded"] if len(present) < 2 else [])
            assert "private-answer" not in json.dumps(broker.fields)

    asyncio.run(run())
