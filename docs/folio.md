# Folio

Folio collects personal account and purchase data behind a local, read-only API.
It runs on the computer holding the account sessions. Callers can read records,
request a sync, and follow its progress; they cannot supply browser URLs,
JavaScript, credentials, or arbitrary HTTP requests.

The repository and Python distribution retain the `ibkr-tws-http` name for
compatibility. The new commands are `folio` and `foliod`. Existing `ibkr-tws`,
`ibkr-tws-http`, configuration files, exposure policies, and Gateway adapter
remain supported.

## What is available

- **IBKR:** collected accounts, account-summary values, and positions. All existing
  live contract, quote, option, and historical-bar endpoints remain available.
- **Bank of Georgia personal banking:** browser collection of accounts,
  per-currency balances, and transactions in the internet banking page's date range.
- **Wolt customer accounts:** paginated order history with merchant, status,
  display total, and item names/quantities.

**BoG and Wolt are experimental.** Their private website response formats were
inspected on 2026-10-12. Tests use real Chromium with local fixture responses;
no authenticated BoG or Wolt account has yet been used to validate these adapters.
A provider change or unfamiliar login flow produces an explicit failure and
preserves the previous snapshot. This is not an implementation of BoG's business
API or Wolt's merchant API.

Wolt's order-list response supplies a formatted total rather than an ISO currency
code, so `display_total` is preserved and `amount` is null. Folio does not guess
currencies or minor-unit scales. Per-item prices, fee/refund breakdowns, downloadable
receipts/statements, and matching a Wolt order to a bank transaction are not yet
implemented. No payments, trades, order placement, or account-setting operations
are exposed.

## Persistent installation

On Arch/Manjaro install `uv` and `chromium`, then run from the checkout:

```sh
python3 scripts/setup-folio.py
```

To replace an existing installation of the IBKR HTTP user service:

```sh
python3 scripts/setup-folio.py --replace-ibkr-service
```

This imports the existing IBKR configuration by reference, retains its exposure
policy, replaces the HTTP service with `folio.service`, and leaves the Gateway
adapter running. It enables user lingering for boot/logout persistence. Use
`--no-linger` if you manage that separately. The legacy service is restored if
Folio fails to start. Never run both HTTP services with the same IBKR client ID.
The default HTTP port is 8080; migration preserves an existing custom port.
Use `--port PORT` to choose a port for a new configuration.

Configuration lives at `~/.config/folio/config.toml`; existing configuration is
preserved. Data and private Chromium profiles live at `~/.local/state/folio/`.
`XDG_CONFIG_HOME` and `XDG_STATE_HOME` are respected. These directories contain
financial data and session credentials; keep backups private.

The CLI is installed in `.venv/bin`. Examples below assume that directory is on
`PATH`; alternatively run `uv run folio ...` from the checkout.

```sh
folio login bog
folio login wolt
folio sync bog
folio sync wolt
folio connections
folio read bog transactions --since 2026-10-01
folio read wolt orders
```

`login` opens a dedicated visible Chromium profile. Complete login/MFA in that
browser, then press Enter in the terminal to close it. On a VM this requires its
desktop session or an appropriately forwarded display. Profiles are locked:
interactive login and background sync cannot use the same profile simultaneously.
The saved session is reused by later headless syncs.

For login forms through zond, install using `--zond` (combine it with
`--replace-ibkr-service` when migrating). Setup adds fixed `folio-bog` and
`folio-wolt` credential adapters to the existing zond configuration. For an
existing Folio config, first set `credential_adapter = true` on each browser
connection. These connections are processed by the configured adapter, not the
HTTP daemon. Both services must be running.

A sync that encounters a recognized password/email or one-time-code form opens
an application-input request in zond. Answers use its private adapter channel,
are filled only on the provider's allowed origin, and are never stored in the
Folio database or logs. Each credential step is submitted at most once per sync.
A decline or unsuccessful login does not automatically retry credentials. Zond's
adapter Retry action explicitly starts a new attempt. CAPTCHA, QR, magic-link,
or otherwise unfamiliar flows require `folio login` and a subsequent sync.
The account session itself can have broader authority than Folio's public API;
the configured adapter executable and its Chromium profile must be trusted.

## Manual run and configuration

```sh
uv sync --locked
mkdir -p ~/.config/folio
cp folio.example.toml ~/.config/folio/config.toml
uv run foliod --check
uv run foliod
```

Do not overwrite an existing config with the example. Add multiple named
connections as needed; names must contain lowercase letters, numbers, `_`, or
`-`, starting with a letter. Each browser connection has its own profile. Each
IBKR connection points to an ordinary IBKR config and needs a distinct TWS client
ID when sharing a Gateway.

The example uses system Chromium with its sandbox enabled. Alternatively omit
`browser.executable` and run `uv run playwright install chromium` once to download
Playwright's compatible browser. The daemon does not download software at startup.

## Sync and freshness

Reads return stored data and make no provider requests. An unsynced resource has
`snapshot: null`. `folio connections` shows the last successful snapshots, current
errors, worker type, and the earliest next sync time.

`folio sync CONNECTION` queues a durable job and follows it by default. Concurrent
requests for the same connection share that job. `--detach` returns its ID
immediately; `folio job ID --follow` resumes watching. Ctrl-C detaches the CLI
without cancelling the job.

Only explicit sync requests contact browser providers. The default minimum
interval is five minutes; pagination is paced at one page per second. A 429
pauses subsequent jobs for at least Retry-After (up to a day). Failed jobs are
not automatically resubmitted. A worker restart marks an interrupted job failed
and retains queued jobs. Zond adapter Retry is an explicit operator override of
the cooldown.

Each successful sync is committed atomically. Account/balance/position snapshots
replace previous current values. Histories merge by provider IDs and retain
previously collected records; a truncated page range never deletes older history.
Records and their snapshot metadata are committed together. Failure at any page
leaves the previous data and freshness untouched.

`max_pages` bounds each sync (default 100). `snapshot.complete` means pagination
finished **within the provider page's selected range**, not that the entire account
lifetime was imported. BoG's original range is recorded in `snapshot.coverage`;
Folio does not silently extend it. Wolt localized timestamps remain source text
when they cannot be safely interpreted. `read --since` treats unzoned input as
UTC and includes undated records with `occurred_at: null` and
`undated_included: true`, rather than silently dropping them. Money amounts are
decimal strings paired with a currency code when the provider supplies one.

## HTTP interface

The new service binds only to `127.0.0.1`; it has no remote authentication or TLS.
Invoke the CLI through zond/SSH for remote use. Requests from unrelated browser
origins and unexpected Host names are rejected. Local processes under other
accounts can reach the loopback listener, so use a trusted machine.

```text
GET  /v1/connections
POST /v1/connections/{name}/sync              JSON body: {}
GET  /v1/jobs/{id}
GET  /v1/connections/{name}/records/{resource}?since=...&limit=100&offset=0
```

Follow `next_offset` to paginate stored records. There is no public profile,
password-answer, browser-control, arbitrary URL, or CDP endpoint.

An IBKR connection also exposes the existing typed live API under
`/v1/connections/{name}/ibkr/v1/...`. The first configured IBKR connection serves
legacy `/v1/...` routes at the root, so `ibkr-tws` still works. These live routes
retain their existing caches and exposure checks; they do not create historical
Folio records implicitly. Each IBKR sync obeys the same policy, including aliases
and account-ID redaction. Restart Folio after changing configuration; a changed
IBKR configuration invalidates its persisted snapshots before they can be read.

## Verification

```sh
uv sync --locked --extra dev
uv run ruff check .
uv run pytest -q
FOLIO_TEST_ZONDD=/path/to/zondd FOLIO_TEST_HEADED=1 \
  xvfb-run -a uv run pytest -q tests/test_folio_zond_e2e.py
```

Browser tests run the installed Chromium with locally intercepted fixture traffic.
The optional zond test starts a real daemon and a visible Chromium instance,
declines a form, retries, submits a fixture password and OTP, and checks that the
secret is absent from daemon logs. These tests do not contact real accounts.
