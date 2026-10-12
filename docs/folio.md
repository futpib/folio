# Folio

Folio collects personal account and purchase data behind a local, read-only API.
It runs on the computer holding the account sessions. Callers can read records,
request a sync, and follow its progress; they cannot supply browser URLs,
JavaScript, credentials, or arbitrary HTTP requests.

The repository is named `folio`. The Python distribution retains the
`ibkr-tws-http` name for compatibility. The new commands are `folio` and `foliod`.
Existing `ibkr-tws`,
`ibkr-tws-http`, configuration files, exposure policies, and Gateway adapter
remain supported.

## What is available

- **IBKR:** collected accounts, account-summary values, and positions. All existing
  live contract, quote, option, and historical-bar endpoints remain available.
- **Binance:** Spot balances and per-symbol trades; USD-M and optional COIN-M
  Futures balances, open positions, trades, and income including funding fees,
  commissions and realized P&L. Uses signed REST API reads with Ed25519, RSA or HMAC credentials
  read from `.env.local` or requested through Zond when missing.
  See [Binance setup](#binance-spot-and-futures).
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

A sync that encounters a recognized password/email form first reads local
credentials as described below. Missing values and one-time codes open
an application-input request in zond. Answers use its private adapter channel,
are filled only on the provider's allowed origin, and are never stored in the
Folio database or logs. Each credential step is submitted at most once per sync.
A decline or unsuccessful login does not automatically retry credentials. Zond's
adapter Retry action explicitly starts a new attempt. CAPTCHA, QR, magic-link,
or otherwise unfamiliar flows require `folio login` and a subsequent sync.
The account session itself can have broader authority than Folio's public API;
the configured adapter executable and its Chromium profile must be trusted.

## Local credentials and Zond fallback

Put credentials in `.env.local` at the **root of this Folio checkout**, alongside
`pyproject.toml`. This path does not depend on the shell or service's working
directory. The file is ignored by Git. [`.env.local.example`](../.env.local.example)
lists the supported names:

```dotenv
BINANCE_API_KEY=
BINANCE_API_SECRET=
BINANCE_PRIVATE_KEY_FILE=
BINANCE_PRIVATE_KEY=
BINANCE_PRIVATE_KEY_PASSPHRASE=
BOG_USERNAME=
BOG_PASSWORD=
WOLT_EMAIL=
WOLT_USERNAME=
WOLT_PASSWORD=
IBKR_USERNAME=
IBKR_PASSWORD=
```

Fill in the values you want to keep locally; leave the others blank or omit them.
If the file is absent, login uses Zond. If only some required values are present,
Zond asks **only for the missing fields**. Configure browser connections with
`credential_adapter = true` and run `scripts/setup-folio.py --zond` for this fallback.
IBKR uses its separate Gateway adapter. One-time codes remain interactive, and
IBKR Mobile approval still happens in IBKR Mobile. Wolt uses `WOLT_EMAIL` for its
email form and username/password values only if those controls appear.

Use one `NAME=value` per line; single or double quotes, `export`, and trailing
comments are supported. Quote passwords containing spaces or `#`. Values are
literal: `$variables`, `${expressions}`, and shell commands are never expanded.
Inside quotes, backslash escapes the matching quote or another backslash.
For Binance PEM material only, literal `\n` sequences become newlines after
loading; passwords and passphrases stay literal.
The file is read directly, without exporting secrets to process environment
variables. Each adapter receives only its provider's entries. Keep the file
readable only by its owner, for example with `chmod 600 .env.local`.

Zond answers stay in memory; Folio does not write them back to `.env.local`,
SQLite, or logs. Browser and Gateway logins reread the file at each attempt.
Binance caches valid keys in its worker until restart or authentication rejection;
edit the file and restart the worker to replace valid cached keys immediately.
After rejection, correct the file and retry: existing values are not replaced by
a Zond prompt. Malformed or unreadable files fail explicitly without echoing
their contents. These names are shared by connections using the same provider;
separate accounts need separate installations/credential files.

## Binance Spot and Futures

Add a named connection to `~/.config/folio/config.toml`:

```toml
[connections.binance]
provider = "binance"
credential_adapter = true
min_sync_seconds = 300
max_pages = 100

[connections.binance.binance]
key_type = "auto"
markets = ["spot", "usdm"]
spot_symbols = ["BTCUSDT", "ETHUSDT"]
usdm_symbols = ["BTCUSDT"]
history_days = 7
```

`usdm` is USD-M Futures. To include COIN-M contracts, add `"coinm"` to `markets`
and configure e.g. `coinm_symbols = ["BTCUSD_PERP"]`. Choose only markets enabled
for your account; a rejected market fails the sync and preserves the previous
snapshot. Standard Binance.com accounts are supported; Binance.US, testnet,
Portfolio Margin and Earn/Funding wallets use different APIs and are not included.

```sh
uv run foliod --check
python3 scripts/setup-folio.py --zond
uv run folio sync binance
# Complete any missing API key fields in Zond, then read the stored results:
uv run folio read binance balances
uv run folio read binance positions
uv run folio read binance trades --since 2026-10-01
uv run folio read binance income --since 2026-10-01
```

Setup registers `folio-binance` using `folio api-adapter binance`, with fixed
configuration and executable paths. A Binance-only configuration needs no
Chromium installation. The Folio daemon and Zond adapter must both be running.
For manual setup, register that command using the existing Zond credential
adapter mechanism and restart Folio after adding the connection.

For a new key, choose **Self-generated** on Binance and use **Ed25519**.
Binance [recommends Ed25519 and marks HMAC deprecated](https://github.com/binance/binance-spot-api-docs/blob/master/faqs/api_key_types.md).
Run the key-generation script from the checkout **on the machine running Folio**,
even before configuring a connection or starting the service:

```sh
python3 scripts/binance-keygen.py
```

The script uses `uv` to install the checkout's locked dependencies and runs
`folio binance-keygen`. It needs neither a running daemon nor a Binance API key.
It always selects its own checkout, even when invoked from another directory.

This prints only the public PEM key. Paste the complete output, including the
`BEGIN PUBLIC KEY` and `END PUBLIC KEY` lines, into Binance. Folio saves the
private key as `.secrets/binance-ed25519.pem` with owner-only permissions and
registers its relative path as `BINANCE_PRIVATE_KEY_FILE` in `.env.local`.
Both locations are ignored by Git. Repeating the command prints the same public
key; it does not rotate or overwrite the private key. Other credentials and
comments in `.env.local` are preserved. A different configured private key is
never replaced. `--type rsa` generates a 2048-bit RSA key instead, and
`--env-file PATH` selects another credential file and adjacent `.secrets` directory.

When Binance supplies the API key, set `BINANCE_API_KEY` in `.env.local`, or
leave it empty to enter it through Zond. The generated private key is unencrypted;
access is controlled by owner-only file and directory permissions. Keep it with
your private backups: the public key cannot recreate it.

Existing Ed25519 or RSA keys can be supplied through `BINANCE_PRIVATE_KEY_FILE`
(absolute, home-relative, or relative to `.env.local`) or `BINANCE_PRIVATE_KEY`
(PKCS#8 PEM on one line using literal `\n` separators). RSA keys must be 2048 or
4096 bits. For an encrypted PKCS#8 file, supply `BINANCE_PRIVATE_KEY_PASSPHRASE`
or let Zond request it. Zond requests only missing values; if an encrypted key
is entered interactively, its passphrase is a subsequent masked step. A missing
referenced key file falls back to Zond; an unreadable or malformed one fails
without printing its contents. Only signatures and the API key go to Binance.

`key_type = "auto"` detects the supplied key. A configured private key takes
precedence over a legacy HMAC secret. With no private-key configuration, Zond's
signing-key field accepts either an HMAC secret or a PEM with literal `\n`
separators. Set `key_type = "ed25519"` or `"rsa"` to enforce that type and request
a private key even when `.env.local` is entirely absent. Existing system-generated
keys continue to work with `BINANCE_API_SECRET`; `key_type = "hmac"` explicitly
selects that legacy mode.

Enable reading and use the minimum permissions necessary for the selected accounts;
leave withdrawals and transfers disabled. Folio does not accept secrets in TOML,
CLI arguments, process environment variables or its HTTP API.
The configured adapter sees the secrets and must be trusted.
Folio exposes only the fixed read endpoints listed below, even when a supplied
key has broader account permissions.

Keys are held in the adapter's memory and reused until it stops or Binance
rejects authentication. They are never copied into SQLite or written to a file.
After restart, the next sync rereads `.env.local` and asks through Zond for any
missing values. Decline, expiry or failure ends the job;
Zond's Retry action starts a fresh attempt. Network failures retain valid in-memory
keys. The Spot account endpoint is read even for Futures-only connections so its
UID can bind the snapshot to an account; Spot account reading must be permitted.
A successful sync with a different UID replaces the prior account's stored data
atomically, preventing histories from two accounts from being mixed.

History covers the configured lookback (`history_days`, 1–90, default 7), subject
to Binance's retention. Select every symbol whose trades you need: Binance's
trade endpoints require a symbol, including for contracts you already closed.
An empty symbol list skips that market's trades; it does not discover past trading
pairs. Futures income covers **all symbols** within the lookback, independent of
the trade symbol list. There is no order-placement, cancellation, withdrawal,
transfer or account-setting endpoint.

Each sync reports its markets, trade symbols and exact UTC range in
`snapshot.coverage`. `complete` applies only to those selections, not all account
history. Spot trade windows are at most one day; Futures windows are at most seven
days. Full windows are subdivided to avoid skipping trades with equal timestamps.
`max_pages` limits the total number of history requests across markets. Exhausting
it, or an indivisible millisecond containing a full page, produces
`complete: false`; increase the limit or narrow the selection. Previously stored
history is retained. Any API error discards the entire new snapshot.

Decimal strings are preserved exactly. Crypto asset codes are not constrained to
three-letter fiat currencies. Futures position IDs include the market, symbol
and hedge side; COIN-M quantities are labelled as contracts. Per-asset balances
are not summed across currencies. Income IDs include their type so funding and
realized P&L records cannot overwrite each other. Positions are snapshots, not a
live risk feed. `read --since` filters stored history; it does not extend a sync's
lookback or trigger a provider request.

Requests use HTTPS to fixed `api.binance.com`, `fapi.binance.com` and
`dapi.binance.com` origins, server-time-based timestamps and a 5-second receive
window. Proxy environment settings are ignored and redirects are refused. API
requests are paced at one per second per worker. HTTP 429/418 responses stop the
sync and defer later jobs for `Retry-After`, including bans of up to three days.
Operator Retry explicitly overrides that cooldown; wait out an active ban.

The implementation follows Binance's official
[Spot signing/account/trade API](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md),
[USD-M account and income API](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/account),
[USD-M trade and position API](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade),
[COIN-M account and income API](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-coin-m-futures/api/rest-api/account),
and [COIN-M trade and position API](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-coin-m-futures/api/rest-api/trade).
It calls only `GET`: each market's `time` and `account`, Spot `myTrades`, Futures
`positionRisk`, `userTrades` and `income`. Tests verify HMAC and public-key
signatures independently, cross-check Ed25519/RSA signatures with OpenSSL, and
exercise a local API fixture with the real Folio CLI, worker and Zond broker.
Authenticated live account access has not yet been verified.

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
FOLIO_TEST_ZONDD=/path/to/zondd uv run pytest -q tests/test_binance_zond_e2e.py
```

Browser tests run the installed Chromium with locally intercepted fixture traffic.
The optional zond test starts a real daemon and a visible Chromium instance,
declines a form, retries, submits a fixture password and OTP, and checks that the
secret is absent from daemon logs. These tests do not contact real accounts.
