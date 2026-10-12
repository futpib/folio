"""Real Chromium/zond test adapter. All browser traffic is intercepted locally."""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

from folio.broker import Broker
from folio.browser import authenticate, browser
from folio.config import Connection, Settings


async def main():
    root = Path(sys.argv[1])
    connection = Connection("bog", "bog")
    settings = Settings(root, (connection,), chromium=shutil.which("chromium"))
    broker = Broker()
    await broker.start()
    try:
        async with browser(
            settings, connection, headed=os.getenv("FOLIO_TEST_HEADED") == "1"
        ) as ctx:

            async def route(request):
                await request.fulfill(
                    content_type="text/html",
                    body="""
                    <form id="login" onsubmit="event.preventDefault();
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
            assert not await authenticate(page, "bog", broker)  # user declines
            (root / "declined").touch()
            await broker.status("unavailable")
            await broker.retry.wait()
            broker.retry.clear()
            assert await authenticate(page, "bog", broker)
            assert await authenticate(page, "bog", broker)
            (root / "authenticated").touch()
            await broker.status("ready")
            await asyncio.Event().wait()
    finally:
        broker.close()


if __name__ == "__main__":
    asyncio.run(main())
