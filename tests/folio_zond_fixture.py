"""Standalone Folio with real Chromium and locally intercepted login traffic."""

from __future__ import annotations

import os
from pathlib import Path

from folio.browser import BrowserConnector, authenticate, browser
from folio.cli import daemon
from folio.models import LoginRequired, Snapshot


async def sync(self, since):
    root = Path.cwd()
    async with browser(
        self.settings, self.connection, headed=os.getenv("FOLIO_TEST_HEADED") == "1"
    ) as ctx:

        async def route(request):
            await request.fulfill(
                content_type="text/html",
                body="""
                    <form id="login" onsubmit="event.preventDefault();
                      if(this.querySelector('[name=username]').value !== 'fixture-user' ||
                         this.querySelector('[type=password]').value !== 'folio-test-secret')
                         return;
                      document.getElementById('otp').hidden=false;this.remove()">
                      <input name="username"><input type="password">
                      <button type="submit">Sign in</button>
                    </form>
                    <form id="otp" hidden onsubmit="event.preventDefault();this.remove()">
                      <input autocomplete="one-time-code">
                      <button type="submit">Verify</button>
                    </form>""",
            )

        await ctx.route("**/*", route)
        page = await ctx.new_page()
        await page.goto("https://account.bog.ge/auth/login")
        for _ in range(2):
            if not await authenticate(page, "bog", self.broker, env_file=root / ".env.local"):
                raise LoginRequired("Fixture login declined")
        return Snapshot(
            records=[], resources=list(self.connection.resources), complete=True, coverage="fixture"
        )


if __name__ == "__main__":
    BrowserConnector.sync = sync
    daemon()
