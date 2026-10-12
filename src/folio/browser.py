from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import TimeoutError as BrowserTimeout
from playwright.async_api import async_playwright

from .config import Connection, Settings
from .credentials import CredentialFileError, read_credentials
from .models import ConnectorError, LoginRequired, RateLimited, SchemaChanged
from .observability import Recorder, browser_logging, current
from .providers import (
    LOGIN_ORIGINS,
    PAGES,
    next_url,
    origin,
    parse,
    resource_for,
    snapshot,
)
from .store import Store, exclusive, private_directory

MAX_BODY = 8 * 1024 * 1024


def retry_delay(value: str | None) -> int:
    try:
        return int(value or "300")
    except ValueError:
        try:
            return max(
                60, int(parsedate_to_datetime(value).timestamp() - datetime.now(UTC).timestamp())
            )
        except (TypeError, ValueError):
            return 300


@asynccontextmanager
async def browser(settings: Settings, connection: Connection, *, headed=False):
    root = settings.state_dir / "profiles"
    private_directory(root)
    profile = root / connection.id
    private_directory(profile)
    try:
        with exclusive(root / f"{connection.id}.lock"):
            async with async_playwright() as playwright:
                context = await playwright.chromium.launch_persistent_context(
                    str(profile),
                    executable_path=settings.chromium,
                    headless=not headed,
                    chromium_sandbox=True,
                    accept_downloads=False,
                    locale="en-US",
                    service_workers="block",
                )
                close_logs = browser_logging(
                    context, current.get() or Recorder(Store(settings.state_dir), connection)
                )
                try:
                    yield context
                finally:
                    close_logs()
                    await context.close()
    except BlockingIOError:
        raise LoginRequired(
            "Browser profile is in use; close Folio login before syncing."
        ) from None


async def authenticate(page, provider, broker, attempted=None, *, env_file=None) -> bool:
    attempted = attempted if attempted is not None else set()
    # Only recognized login forms on provider-owned origins can receive answers.
    for frame in page.frames:
        page_origin = origin(frame.url)
        if page_origin not in LOGIN_ORIGINS[provider]:
            continue
        password = frame.locator('input[type="password"]:visible')
        email = frame.locator('input[type="email"]:visible')
        otp = frame.locator('input[autocomplete="one-time-code"]:visible')
        fields, locators = [], []
        if await password.count() == 1:
            username = frame.locator(
                'input[name="username"]:visible, input[autocomplete="username"]:visible'
            )
            if await username.count() == 1:
                fields.append({"id": "username", "label": "Username", "kind": "text"})
                locators.append(username)
            fields.append({"id": "password", "label": "Password", "kind": "password"})
            locators.append(password)
        elif await otp.count() == 1:
            fields.append({"id": "otp", "label": "Sign-in code", "kind": "otp"})
            locators.append(otp)
        elif provider == "wolt" and await email.count() == 1:
            fields.append({"id": "email", "label": "Email address", "kind": "text"})
            locators.append(email)
        else:
            continue
        submit = frame.locator('button[type="submit"]:visible, input[type="submit"]:visible')
        if await submit.count() != 1:
            continue
        kind = fields[-1]["id"]
        if kind in attempted:
            return False
        try:
            local = read_credentials(provider, env_file)
        except CredentialFileError as error:
            raise LoginRequired(str(error)) from None
        values = {field["id"]: local[field["id"]] for field in fields if field["id"] in local}
        local.clear()
        missing = [field for field in fields if field["id"] not in values]
        if missing and broker is None:
            values.clear()
            return False
        attempted.add(kind)
        elements = [await locator.element_handle() for locator in locators]
        submit_element = await submit.element_handle()
        page_url = frame.url
        ident = None
        answer_task = None
        delivered = False
        resolved = False
        try:
            if missing:
                ident = await broker.challenge(provider, missing, page_origin)
                answer_task = asyncio.create_task(broker.receive())
                while not answer_task.done():
                    await asyncio.wait({answer_task}, timeout=1)
                    if frame.url != page_url or not all(
                        [await locator.is_visible() for locator in locators]
                    ):
                        if answer_task.done():
                            supplied = answer_task.result().get("values")
                            delivered = bool(supplied)
                            if delivered:
                                supplied.clear()
                                await broker.result(ident, "cancelled")
                        else:
                            answer_task.cancel()
                            await asyncio.gather(answer_task, return_exceptions=True)
                            await broker.withdraw(ident)
                        resolved = True
                        return False
                answer = answer_task.result()
                if answer.get("challenge_id") != ident or answer.get("type") != "answered":
                    raise RuntimeError("unexpected credential response")
                supplied = answer.pop("values", None)
                delivered = bool(supplied)
                if not supplied:
                    resolved = True
                    return False
                try:
                    values.update({field["id"]: supplied[field["id"]] for field in missing})
                finally:
                    supplied.clear()
            # Recipient validation and filling happen in one browser task, using the
            # original elements. Navigation cannot retarget a waiting locator to a new form.
            try:
                await frame.evaluate(
                    """({url, fields, submit}) => {
                    if (location.href !== url || !submit?.isConnected || submit.disabled)
                        throw new Error('Login form changed');
                    const form = submit.form;
                    if (!form || new URL(form.action, location.href).origin !== location.origin)
                        throw new Error('Unexpected form recipient');
                    if (fields.some(f => !f.element?.isConnected || f.element.form !== form ||
                            !f.element.getClientRects().length || f.element.disabled))
                        throw new Error('Login form changed');
                    const set = Object.getOwnPropertyDescriptor(
                        HTMLInputElement.prototype, 'value').set;
                    for (const field of fields) {
                        set.call(field.element, field.value);
                        field.element.dispatchEvent(new Event('input', {bubbles: true}));
                        field.element.dispatchEvent(new Event('change', {bubbles: true}));
                    }
                    submit.click();
                }""",
                    {
                        "url": page_url,
                        "submit": submit_element,
                        "fields": [
                            {"element": element, "value": values[field["id"]]}
                            for field, element in zip(fields, elements, strict=True)
                        ],
                    },
                )
            finally:
                values.clear()
            await page.wait_for_timeout(2000)
            success = not await locators[-1].is_visible()
            if ident:
                await broker.result(ident, "succeeded" if success else "failed")
            resolved = True
            return success
        finally:
            values.clear()
            if ident and not resolved:
                if not answer_task.done():
                    answer_task.cancel()
                    await asyncio.gather(answer_task, return_exceptions=True)
                    await broker.withdraw(ident)
                elif delivered:
                    await broker.result(ident, "cancelled")
    return False


class BrowserConnector:
    def __init__(self, settings: Settings, connection: Connection, broker=None, *, env_file=None):
        self.settings, self.connection, self.broker = settings, connection, broker
        self.env_file = env_file

    async def sync(self, since: str | None):
        async with browser(self.settings, self.connection) as context:
            return await self.collect(context, since)

    async def collect(self, context, since: str | None):
        provider = self.connection.provider
        page = await context.new_page()
        records, complete, coverage = [], True, []
        for target in PAGES[provider]:
            resource = (
                "orders"
                if provider == "wolt"
                else ("accounts" if target.endswith("accounts") else "transactions")
            )
            captured = asyncio.Queue()

            def on_response(response, captured=captured, resource=resource):
                if resource_for(provider, response.url, response.request.method) == resource:
                    captured.put_nowait(response)

            page.on("response", on_response)
            try:
                await page.goto(target, wait_until="domcontentloaded", timeout=45000)
                attempted = set()
                revisited = False
                for stage in range(5):
                    try:
                        response = await asyncio.wait_for(captured.get(), 15 if stage == 0 else 3)
                        payload = await self.response_json(response)
                        break
                    except (TimeoutError, LoginRequired):
                        if provider == "wolt" and stage == 0:
                            login = page.get_by_role("button", name="Log in", exact=True)
                            if await login.count() == 1:
                                await login.click()
                        if await authenticate(
                            page, provider, self.broker, attempted, env_file=self.env_file
                        ):
                            continue
                        if attempted and not revisited:
                            revisited = True
                            await page.goto(target, wait_until="domcontentloaded", timeout=45000)
                            continue
                        raise LoginRequired(
                            "Sign in with 'folio login CONNECTION' or finish the zond login form."
                        ) from None
                else:
                    raise LoginRequired("Login needs attention in 'folio login CONNECTION'.")
                initial_url = response.url
                headers = await response.request.all_headers()
                headers = {
                    k: v
                    for k, v in headers.items()
                    if k.lower()
                    in {
                        "authorization",
                        "x-xsrf-token",
                        "x-csrf-token",
                        "x-auth-token",
                        "x-ext-customer-id",
                        "platform",
                        "client-version",
                        "clientversionnumber",
                        "app-language",
                        "app-locale",
                        "client-language",
                        "accept-language",
                    }
                }
                if resource == "accounts":
                    records.extend(parse(provider, resource, payload))
                    coverage.append("current accounts and balances")
                    continue
                # Start at the page's original range; retain that range in freshness metadata.
                query = parse_qs(urlsplit(initial_url).query)
                coverage.append(
                    "order history"
                    if provider == "wolt"
                    else "BoG page range: "
                    + str(
                        {
                            k: v
                            for k, v in query.items()
                            if k in {"operationDateTimeLowerBound", "operationDateTimeUpperBound"}
                        }
                    )
                )
                seen = set()
                for index in range(self.connection.max_pages):
                    batch = parse(provider, resource, payload)
                    records.extend(batch)
                    if provider == "wolt":
                        token = payload.get("next_page_token")
                        if not token:
                            break
                        url = next_url(initial_url, page_token=token)
                    else:
                        size = int(query.get("limit", [25])[0])
                        if len(batch) < size:
                            break
                        cursor = payload["data"][-1].get("sort")
                        if not isinstance(cursor, list) or not cursor:
                            raise SchemaChanged("BoG pagination cursor is missing.")
                        url = next_url(initial_url, searchAndSort=",".join(map(str, cursor)))
                    if url in seen:
                        raise SchemaChanged("Provider repeated a pagination cursor.")
                    seen.add(url)
                    if index + 1 == self.connection.max_pages:
                        complete = False
                        break
                    await asyncio.sleep(1)
                    payload = await self.fetch(page, provider, resource, url, headers)
            except (TimeoutError, BrowserTimeout):
                raise LoginRequired(
                    "History did not load. Check the browser session with 'folio login CONNECTION'."
                ) from None
            finally:
                page.remove_listener("response", on_response)
        return snapshot(provider, records, complete, "; ".join(coverage))

    async def response_json(self, response):
        if response.status in {401, 403}:
            raise LoginRequired("Provider login expired or access was refused.")
        if response.status == 429:
            raise RateLimited(retry_delay(response.headers.get("retry-after")))
        if response.status != 200:
            raise ConnectorError("Provider history request failed.")
        if len(await response.body()) > MAX_BODY:
            raise SchemaChanged("Provider response exceeded the size limit.")
        try:
            payload = json.loads(await response.body(), parse_float=Decimal)
        except ValueError:
            raise SchemaChanged("Provider did not return JSON history.") from None
        if not isinstance(payload, dict):
            raise SchemaChanged("Provider returned an unexpected history format.")
        if self.connection.provider == "bog" and payload.get("code") == 20:
            raise LoginRequired("BoG login expired.")
        return payload

    async def fetch(self, page, provider, resource, url, headers):
        if resource_for(provider, url) != resource:
            raise ConnectorError("Refused a request outside the provider's history endpoint.")
        # Credentials stay inside the worker. Neither callers nor stored records receive headers.
        result = await page.evaluate(
            """async ({url, headers, max}) => {
            const response = await fetch(url, {headers, credentials: 'include', redirect: 'error',
                signal: AbortSignal.timeout(30000)});
            const text = await response.text();
            return {status: response.status, retry: response.headers.get('retry-after'),
                    body: text.length <= max ? text : null};
        }""",
            {"url": url, "headers": headers, "max": MAX_BODY},
        )
        if result["status"] == 429:
            raise RateLimited(retry_delay(result["retry"]))
        if result["status"] in {401, 403}:
            raise LoginRequired("Provider login expired or access was refused.")
        if result["status"] != 200 or result["body"] is None:
            raise ConnectorError("Provider pagination request failed.")
        try:
            payload = json.loads(result["body"], parse_float=Decimal)
        except ValueError:
            raise SchemaChanged("Provider pagination format changed.") from None
        if not isinstance(payload, dict):
            raise SchemaChanged("Provider pagination format changed.")
        return payload


async def login(settings: Settings, connection: Connection):
    async with browser(settings, connection, headed=True) as context:
        page = await context.new_page()
        await page.goto(PAGES[connection.provider][0], wait_until="domcontentloaded")
        print("Sign in in the browser, then press Enter here to save and close it.")
        await asyncio.to_thread(input)
