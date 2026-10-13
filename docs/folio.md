# Folio

Folio collects personal account and purchase data behind a local, read-only API.
It runs on the computer holding the account sessions. Callers can read records,
request a sync, and follow its progress; they cannot supply browser URLs,
JavaScript, credentials, or arbitrary HTTP requests.

`foliod`, installed as `folio.service`, owns all provider workers. It runs
independently of zondd. Zond is an optional authentication broker: Folio contacts
it only for fields missing from local credentials. Complete `.env.local`
credentials and existing browser sessions work with Zond stopped or absent.

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

## Configuration CLI

Run these commands **on the machine that will run Folio**, from its checkout.
Setup works without a running daemon or an existing Folio configuration:

```sh
uv run folio setup
```

The wizard adds or edits a named connection for Binance, BoG, Wolt or IBKR. It asks
for provider settings, sync limits, the local HTTP port and data directory, then
validates the result and installs/starts the user service. IBKR connections reuse
an existing Gateway/TWS configuration and its exposure policy. Gateway installation
and its first login remain covered by the [IBKR setup guide](../README.md#include-headless-gateway-sign-in-through-zond).
Browser connections need Chromium. If Zond is installed and configured, setup
registers credential recipients and preserves its existing SSH policy and
other recipients. Existing Gateway or custom legacy adapters must first migrate
to their own service using Zond's `credential-adapter` bridge. Without Zond, setup
still starts Folio; missing credentials then produce an actionable login error.

Give a name to edit one connection. Add `--yes` to use existing values or defaults
without prompts; flags change only the selected settings:

```sh
uv run folio setup binance --yes
uv run folio setup binance --yes --markets spot,usdm \
  --spot-symbols BTCUSDT,ETHUSDT --usdm-symbols BTCUSDT --history-days 30
uv run folio setup bank --provider bog --yes --chromium /usr/bin/chromium
uv run folio setup wolt --yes
uv run folio setup ibkr --yes --ibkr-config ~/.config/ibkr-tws-http/config.toml
uv run folio setup --yes --port 9080
```

Binance defaults to Spot and USD-M, a seven-day lookback and automatic signing-key
detection. A fresh setup generates an Ed25519 private key locally, registers its
path in `.env.local`, and displays the public key for Binance's **Self-generated**
API key form. Reruns reuse existing signing credentials; `--key-type rsa` selects
RSA for a fresh setup and `--key-type hmac` skips generation. Missing API credentials
still go through Zond on sync. Trade symbols start empty: balances, positions and
Futures income work, but per-symbol trades require your actual symbols. Pass `--spot-symbols ''` (or the
corresponding Futures option) to clear a list. Disabling a market removes its
configured trade-symbol list. Other provider and connection settings remain unchanged.

`--configure-only` validates and saves, including local Binance key setup, without
changing services. `--no-linger` skips boot/logout persistence setup.
`folio --config PATH setup ...` supports a
custom configuration file. Existing comments are preserved, changes get private
backups, and invalid settings leave the previous file intact. A managed legacy
IBKR service is migrated automatically; custom units and adapter bindings are not
silently replaced. A failed service installation restores the previous config,
Zond configuration and Folio unit, and restarts services that were running.
Generated private keys are retained if service setup fails, so a retry uses the
same public key.

Inspect or edit any supported Folio setting while the daemon is stopped:

```sh
uv run folio config show
uv run folio config set server.port 9080
uv run folio config set connections.binance.binance.markets '["spot", "usdm", "coinm"]'
uv run folio config set connections.binance.binance.coinm_symbols '["BTCUSD_PERP"]'
uv run folio config unset connections.binance.binance.coinm_symbols
uv run folio setup --yes
```

`config set` accepts a TOML value; quote strings and arrays as in the examples.
`config unset` can remove optional fields or a connection, provided the remaining
configuration is valid. Use a new connection name when changing providers.
Run `setup --yes` after individual edits to apply service and recipient changes.
Setup restarts Folio to apply configuration and code changes. It replaces its old
managed `api-adapter`/`browser-adapter`/`credential-relay` entries with public API
registrations and removes registrations for retired Folio connections. Other
recipients and SSH policy are preserved. Existing Gateway and custom legacy
adapters need their own migration before setup can validate the new Zond config.

Enter credentials with masked prompts instead of placing secrets in commands:

```sh
uv run folio credentials binance
uv run folio credentials bog
uv run folio credentials wolt
uv run folio credentials ibkr
```

These commands preserve other entries in the checkout's ignored `.env.local`
and save with owner-only permissions. Enter keeps a value; `-` removes it.
They never display existing secrets. A missing field still falls back to Zond.
Binance setup handles signing keys; `credentials binance`
can also set a path to an existing private key. Inline PEMs remain supported in
`.env.local`. Already cached credentials take effect after worker restart or
authentication rejection, as described below.

After setup, check the connection with `folio sync NAME`, then `folio read NAME RESOURCE`.

## Persistent installation

`folio setup` is the guided interface to the installer below. Existing scripted
installations can continue using these commands.

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
`--replace-ibkr-service` when migrating). Setup adds `folio-bog` and
`folio-wolt` credential recipients to the existing zond configuration. For an
existing Folio config, first set `credential_adapter = true` on each browser
connection. Provider work always runs in Folio. Its lazy public API client
connects directly to Zond for missing credentials. Both services are needed only
when a login requires missing credentials or an interactive code.

A sync that encounters a recognized password/email form first reads local
credentials as described below. Missing values and one-time codes open
an application-input request in zond. Answers use its authenticated private socket,
are filled only on the provider's allowed origin, and are never stored in the
Folio database or logs. Each credential step is submitted at most once per sync.
A decline or unsuccessful login does not automatically retry credentials.
Run `folio sync CONNECTION` for a new attempt. CAPTCHA, QR, magic-link,
or otherwise unfamiliar flows require `folio login` and a subsequent sync.
The account session itself can have broader authority than Folio's public API;
the Folio service, its connection identity and its Chromium profile must be trusted.

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
If the file is absent and login requires credentials, Folio uses Zond.
If only some required values are present, Zond asks **only for the missing fields**.
Configure browser connections with
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
variables. Each connector reads only its provider's entries. Keep the file
readable only by its owner, for example with `chmod 600 .env.local`.

Zond answers stay in memory; Folio does not write them back to `.env.local`,
SQLite, or logs. Browser and Gateway logins reread the file at each attempt.
Binance caches valid keys in Folio until restart or authentication rejection;
edit the file and restart `folio.service` to replace valid cached keys immediately.
After rejection, correct the file and retry: existing values are not replaced by
a Zond prompt. Malformed or unreadable files fail explicitly without echoing
their contents. These names are shared by connections using the same provider;
separate accounts need separate installations/credential files.

## Binance Spot and Futures

For automatic setup:

```sh
uv run folio setup binance --yes
# Register the displayed public key in Binance, then:
uv run folio sync binance
```

Or add a named connection manually to `~/.config/folio/config.toml`:

```toml
[connections.binance]
provider = "binance"
credential_adapter = true
min_sync_seconds = 300
max_pages = 100

[connections.binance.binance]
key_type = "auto"
account_mode = "auto"
markets = ["spot", "usdm"]
spot_symbols = ["BTCUSDT", "ETHUSDT"]
usdm_symbols = ["BTCUSDT"]
history_days = 7
```

`usdm` is USD-M Futures. To include COIN-M contracts, add `"coinm"` to `markets`
and configure e.g. `coinm_symbols = ["BTCUSD_PERP"]`. Choose only markets enabled
for your account; rejected sections produce a partial sync and preserve their
previous values. Binance.com Classic, Portfolio Margin, Portfolio Margin Pro and
Pro SPAN account modes are supported. Binance.US, testnet and Earn/Funding wallets
are separate products and are not included in this connector.

### Binance account modes

`account_mode = "auto"` is the default, including for existing configurations.
It reads `/sapi/v1/portfolio/account` to distinguish `PM_1` (Pro), `PM_2`
(Portfolio Margin) and `PM_3` (Pro SPAN). A response explicitly identifying a
non-Portfolio account selects Classic. When that metadata is denied, Folio tries
`/papi/v1/account`, then falls back to trying the Classic and PAPI read endpoints
independently. A permission rejection is not treated as proof that the account
has no positions. Unknown account types, invalid signatures, network failures
and rate limits fail the sync rather than silently selecting another mode.

- Classic uses Spot API, FAPI for USD-M and DAPI for COIN-M.
- Portfolio Margin uses PAPI `/papi/v1/um/*` and `/papi/v1/cm/*` for Futures
  accounts, positions, trades and income, plus `/papi/v1/account` and
  `/papi/v1/balance` for unified account information.
- Portfolio Margin Pro uses SAPI `/sapi/v1/portfolio/account` and `/balance`
  for unified account information, with the regular FAPI/DAPI Futures reads.
- Pro SPAN additionally reads `/sapi/v2/portfolio/account` for its account summary.

The detected mode appears in verbose logs as `account_mode_detected`, in
snapshot coverage, and in collected account and Futures records. Detection runs
on each sync, so changing account mode does not require replacing keys or
creating another connection. Spot's UID continues to bind history to the account.

An optional `--account-mode` setup argument or TOML setting can select `classic`,
`portfolio`, `portfolio_pro` or `portfolio_pro_span` when account metadata is
unavailable. Selecting a mode does not change anything on Binance:

```sh
uv run folio setup binance --yes --account-mode portfolio
uv run folio sync binance --verbose
```

Unified balances are stored under `market: "portfolio"` with
`balance_scope: "unified"`. Their total and component wallet fields describe
overlapping amounts; do not add them to each other or to the per-market wallet
balances. PAPI market balances retain `crossWalletBalance`, `crossUnPnl` and
margin requirements; Folio does not invent an available balance. Empty optional
amounts are omitted. Denied unified sections retain their last successful data.

Binance [removes the normal Futures permission when Portfolio Margin is enabled](https://www.binance.com/en/support/faq/detail/ccf04078d7774d479f11ae15c4f56081).
Such a key needs PAPI for user data; adding Spot trading permission does not
make FAPI account reads work. Folio supports the read endpoints for each mode
and never places trades. Local credentials still take precedence; Zond is only
used for missing fields. Rerun setup after updating to reconcile the credential
recipient's PAPI origin, then sync on the machine holding your credentials.

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

With Zond configured, `folio setup --yes` registers `folio-binance` as a public API
recipient. All provider workers run inside `foliod`; systemd owns the standalone
Folio service. A Binance-only configuration needs no Chromium installation.
Complete local credentials work with Zond stopped or absent;
`credential_adapter = false` disables the optional fallback entirely. This
configuration name remains compatible with existing Folio connections.

Folio uses Zond's public credential API v1 over `credential.sock`. Setup reads
Zond's runtime directory and writes the absolute endpoint to `[zond].socket` in
Folio's config. It provisions a distinct random identity in
`STATE_DIR/zond/CONNECTION.key` (directory 0700, file 0600), puts only its SHA-256
digest in Zond's `[credential_recipients.folio-CONNECTION]`, and registers the
provider's allowed origins and field IDs. Names longer than 58 characters use a
shortened recipient name with a hash suffix. Reruns reuse the identity. Provider
credentials remain separate in `.env.local` or transient answer values.

Setup authenticates a short test connection without requesting any provider
values, then disconnects. During sync, Folio opens a connection only when a
required field is missing. Challenge and response IDs are correlated, answers
are kept in memory, and cancellation withdraws the form and discards an answer
that was already in flight. Closing the connection invalidates pending input;
a later sync reconnects with a fresh session. Restarting Zond never restarts
Folio or discards Binance credentials already loaded into memory.

The identity authenticates possession of a private connection key, together
with the peer UID. It does not attest the Folio executable or systemd unit.
Keep the key private; software able to read it can use that registration.
No connection key or answer is placed in argv, exported environment values,
Folio's database, or request logs. Setup retains generated identity files if a
later service check fails, while restoring config and service changes.

For a new key, choose **Self-generated** on Binance and use **Ed25519**.
Binance [recommends Ed25519 and marks HMAC deprecated](https://github.com/binance/binance-spot-api-docs/blob/master/faqs/api_key_types.md).
`folio setup binance` handles generation and displays the public key on the
machine where you run it. Run it **on the machine running Folio**. It also
displays the public key for an existing imported key, provided its passphrase
is available locally if encrypted. Existing HMAC credentials and missing signing
material for an already configured API key are retained for Zond fallback; setup
never silently associates an existing API key with a new private key.
An existing connection using only Zond credentials also keeps that behavior.
To explicitly add local key generation to it, run
`folio setup binance --key-type ed25519 --yes`.

For standalone key generation without connection or service setup, the older
entry point remains supported:

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
A registered private key that has gone missing is never regenerated: restore it
from your private backup or supply it through Zond.

When Binance supplies the API key, set `BINANCE_API_KEY` in `.env.local`, or
use `folio credentials binance`, or leave it empty to enter it through Zond.
The generated private key is unencrypted; access is controlled by owner-only
file and directory permissions. Keep it with
your private backups: the public key cannot recreate it.

Existing Ed25519 or RSA keys can be supplied through `BINANCE_PRIVATE_KEY_FILE`
(absolute, home-relative, or relative to `.env.local`) or `BINANCE_PRIVATE_KEY`
(PKCS#8 PEM on one line using literal `\n` separators). RSA keys must be 2048 or
4096 bits. For an encrypted PKCS#8 file, supply `BINANCE_PRIVATE_KEY_PASSPHRASE`
or let Zond request it. Zond requests only missing values; if an encrypted key
is entered interactively, its passphrase is a subsequent masked step. A missing
referenced key file falls back to Zond; an unreadable or malformed one fails
without printing its contents. Only signatures and the API key go to Binance.

When Binance returns a JSON API rejection, the error includes the market,
endpoint, HTTP status and Binance numeric code. Authentication errors distinguish `-2014` (key format), `-2015`
(key/IP/permissions) and `-1022` (signature), as defined in the
[official error reference](https://github.com/binance/binance-spot-api-docs/blob/master/errors.md).
For example, `usdm account (/fapi/v3/account, HTTP 401, code -2015)` identifies
Futures account access as the failed request. Raw provider messages and signed
URLs are not copied into jobs or logs because they can contain secrets. After
updating Folio, run `folio setup binance --yes`, then `folio sync binance` to
produce a new diagnostic; existing failed jobs retain their old error text.

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
The Folio service receives credentials and must be trusted.
Normal Binance sync uses the fixed read endpoints listed below. One-off
`folio call` diagnostics allow other GET endpoints on the supported API origins.

Keys are held in Folio's memory and reused until it stops, all configured reads
are rejected, or Binance reports an invalid key format or signature. They are
never copied into SQLite or written to a file.
After restart, the next sync rereads `.env.local` and asks through Zond for any
missing values. Decline, expiry or failure ends the job;
run `folio sync CONNECTION` for a fresh attempt. Network failures retain valid
in-memory keys. The Spot account endpoint is read even for Futures-only connections
so its UID can bind the snapshot to an account. When that endpoint is denied, a SHA-256
API-key fingerprint binds the data to the current key. Changing keys without a
readable UID clears cached data because Folio cannot verify account continuity.
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
history is retained. Permission rejections allow a partial sync as described below;
other API errors discard the entire new snapshot.

### Partial Binance access

An API key may read Spot while Futures requests return HTTP 401 / `-2015`.
Folio attempts each configured section independently: accounts/balances,
positions, each configured trade symbol, and income. If at least one configured
section succeeds, denied sections are skipped and readable data is saved
atomically. The job returns `state: "completed"`, `complete: false`, and a
`warnings` list identifying each skipped request, its HTTP status and API code.
`--verbose` shows `section_skipped` events alongside individual request logs.

```sh
uv run folio sync binance --verbose
uv run folio read binance balances
uv run folio read binance positions
```

Reads and `connections` include `scopes` with each resource's market, `status`
(`complete`, `incomplete` or `denied`) and last successful `synced` timestamp.
Denied sections keep their previously collected records and timestamps. A section
without a known successful fetch has `synced: null`; a wholly unavailable resource
has no new snapshot. A successful empty balance or position response clears previous
values for that market. Mixed resources can contain fresh and older records;
check each scope and each record's `observed_at` rather than treating the aggregate
snapshot timestamp as freshness for every market.

Valid credentials stay cached after a partial sync. After fixing permissions,
start another sync to retry all configured sections; the normal minimum sync
interval still applies. Zond is used only for credentials missing from `.env.local`.
If you replace a key after a partial sync, restart `folio.service` to load it.

If every configured section is denied, the job still fails with `login_required`
and credentials are discarded so `.env.local` is reread on retry. Invalid key
formats (`-2014`) and signatures (`-1022`) also fail the job. Rate limits, network
errors, malformed responses and other API errors retain the previous complete
stored state; they do not silently become partial successes.

Decimal strings are preserved exactly. Crypto asset codes are not constrained to
three-letter fiat currencies. Futures position IDs include the market, symbol
and hedge side; COIN-M quantities are labelled as contracts. Per-asset balances
are not summed across currencies. Income IDs include their type so funding and
realized P&L records cannot overwrite each other. Positions are snapshots, not a
live risk feed. `read --since` filters stored history; it does not extend a sync's
lookback or trigger a provider request.

Requests use HTTPS to fixed `api.binance.com`, `fapi.binance.com`,
`dapi.binance.com` and `papi.binance.com` origins, server-time-based timestamps and a 5-second receive
window. Proxy environment settings are ignored and redirects are refused. API
requests are paced at one per second per worker. HTTP 429/418 responses stop the
sync and defer later jobs for `Retry-After`, including bans of up to three days.
Later sync requests remain queued until the cooldown expires.

The implementation follows Binance's official
[Spot signing/account/trade API](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md),
[USD-M account and income API](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/account),
[USD-M trade and position API](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade),
[COIN-M account and income API](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-coin-m-futures/api/rest-api/account),
the [COIN-M trade and position API](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-coin-m-futures/api/rest-api/trade),
[Portfolio Margin account API](https://developers.binance.com/en/docs/catalog/advanced-trading-derivatives-trading-portfolio-margin/api/rest-api/account),
[Portfolio Margin trade API](https://developers.binance.com/en/docs/catalog/advanced-trading-derivatives-trading-portfolio-margin/api/rest-api/trade)
and [Portfolio Margin Pro account API](https://developers.binance.com/en/docs/catalog/advanced-trading-derivatives-trading-portfolio-margin-pro/api/rest-api/account).
It calls only `GET`: each market's `time` and `account`, Spot `myTrades`, Futures
`positionRisk`, `userTrades` and `income`, plus Portfolio `account` and `balance`
and Pro SPAN `account`. Tests verify HMAC and public-key
signatures independently, cross-check Ed25519/RSA signatures with OpenSSL, and
exercise a local API fixture with the real Folio CLI, worker and Zond broker.
Tests cover all four account modes with local fixtures. A verbose sync on the
deployment machine verifies that its Binance account and key can read each section.

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

Add `--verbose` to sync or job commands for individual provider requests:

```sh
folio sync binance --verbose
folio job JOB_ID --follow --verbose
folio logs binance --follow
folio logs binance --job JOB_ID
journalctl --user -u folio -f
```

Each request records its method, fixed endpoint, start/completion, elapsed
milliseconds, HTTP status when available, and Binance numeric error code when
present. Job completion includes completeness and a warning count; skipped Binance
sections emit `section_skipped`. Missing-field prompts appear as
`credentials_requested`. Verbose progress goes to stderr so the final
JSON result remains usable on stdout.

Logs are written by Folio, appear in its service journal, and retain the newest
10,000 events per connection in its private SQLite database. `logs --follow`
prints retained events before following new ones. Query strings, headers, bodies,
credential values and raw exception messages are excluded. Browser logging covers
provider-owned document, fetch and XHR traffic; unfamiliar paths are redacted
because they may contain identifiers or tokens. IBKR logs its typed local API
requests using route templates, including live reads outside sync jobs.

Only explicit sync requests contact browser providers. The default minimum
interval is five minutes; pagination is paced at one page per second. A 429
pauses subsequent jobs for at least Retry-After (up to a day). Failed jobs are
not automatically resubmitted. A Folio restart marks an interrupted job failed
and retains queued jobs. Authentication failures allow a fresh sync immediately
after correcting credentials. Start a new Folio sync to retry; Zond only handles
the credential form.

Each successful sync is committed atomically. Account/balance/position snapshots
replace previous current values within successfully read sections. Histories merge
by provider IDs and retain previously collected records; a truncated page range
never deletes older history.
Records and their snapshot metadata are committed together. Failure at any page
leaves the previous data and freshness untouched. Binance permission denials
produce partial successes and preserve inaccessible sections with their old freshness.

`max_pages` bounds each sync (default 100). `snapshot.complete` means pagination
finished **within the provider page's selected range**, not that the entire account
lifetime was imported. BoG's original range is recorded in `snapshot.coverage`;
Folio does not silently extend it. Wolt localized timestamps remain source text
when they cannot be safely interpreted. `read --since` treats unzoned input as
UTC and includes undated records with `occurred_at: null` and
`undated_included: true`, rather than silently dropping them. Money amounts are
decimal strings paired with a currency code when the provider supplies one.

## Single-page live reads

`folio read CONNECTION RESOURCE --live` selects the adapter endpoint by resource
and fetches one native data page. It does not queue a sync, follow pages, join
responses or change settings and snapshots. The caller chooses pagination,
aggregation and interpretation, including lot matching. Live response fields
retain the provider's names; JSON decimal numbers become exact decimal strings.
Cached `read` responses retain their existing normalized record format.

```sh
# Start at the earliest trade ID available to the API:
uv run folio read binance trades --live --market usdm --symbol ETHUSDT \
  --from-id 0 --limit 1000 --verbose
# Resume using next_cursor.from_id from the previous response:
uv run folio read binance trades --live --market usdm --symbol ETHUSDT \
  --from-id 8747264116 --limit 1000
# An explicit UTC window: since is inclusive, until is exclusive for Binance.
uv run folio read binance trades --live --market usdm --symbol ETHUSDT \
  --since 2026-09-12 --until 2026-09-19
uv run folio read binance positions --live --market usdm
uv run folio read binance balances --live --market portfolio
uv run folio read binance income --live --market usdm --page 1 --limit 1000
uv run folio read bog transactions --live --limit 25 --cursor '1,2'
uv run folio read wolt orders --live --cursor PAGE_TOKEN
uv run folio read ibkr positions --live
```

Binance requires `--market spot`, `usdm`, `coinm` or `portfolio`; `portfolio`
selects unified accounts/balances. Trades require `--symbol` and default to
`--from-id 0` when no date window is specified. `--from-id` cannot be combined
with date bounds. Provide both `--since` and `--until` for a fixed trade/income
window; timestamps without a zone use UTC. Trade windows span at most one day
for Spot or seven days for Futures. The response includes `next_cursor`,
`page_full`, `observed_at` and `scope`. A trade-ID cursor is also useful for
resuming future reads after a short page. The caller stops or requests another
page; an empty page has no next cursor. A full time-window page has no ID cursor
because Binance cannot combine it with date bounds: the caller must divide the
window or choose trade-ID pagination. `page_full` describes only the fetched
page and does not certify lifetime-history coverage or remaining page count.

Binance mode selection uses `--account-mode`, then connection settings, then a
mode remembered by a successful sync for the same API key. When `auto` is still
unknown, it makes one account-mode metadata lookup before the one data request.
It does not try alternative history endpoints or paginate automatically. Set a
known mode explicitly to guarantee one Binance API request. Signing uses the
same in-memory/local credentials and Zond fallback as `folio call`.

BoG transactions use `--cursor` for the native sort cursor and support the
captured API's date-bound fields. BoG accounts/balances return the same native
response, preserving separate account/detail sections for the caller. Wolt
orders return the native `next_page_token` as `next_cursor.cursor`, using the
provider's page size. IBKR snapshots retain the local API's exposure policy.
Browser preparation can load pages and complete sign-in before the requested
data page. `--limit` controls Binance history and BoG transactions; snapshot
endpoints and Wolt do not expose that filter. Unsupported filters fail before
authentication. BoG/Wolt remain experimental for authenticated real accounts.

For readable reusable Zond access, authorize the normal resource command with
fixed provider, market and account mode, and typed symbol/cursor parameters:

```text
rule binance-history(symbol: repo-name, cursor: repo-name) {
  run uv run --no-sync --frozen --directory "$HOME/code/folio" folio read binance trades --live --market usdm --account-mode portfolio --symbol "$symbol" --from-id "$cursor" --limit 1000
}
```

This grants one USD-M Portfolio Margin history page per invocation, with no
arbitrary URL, headers or API operation. Folio validates the symbol and numeric
cursor before accessing credentials or the provider. A caller can invoke
`binance-history ETHUSDT 0`, save the result, and use its cursor in the next
invocation. Lot reconstruction then runs in the caller's own process; it needs
no additional remote execution authority. Keep arbitrary `folio call` smoke
tests on individual command approvals.

## One-off API calls

`folio call CONNECTION ENDPOINT` runs one requested live call through the adapter.
It uses the running Folio service, its credentials or browser profile, and the
normal `.env.local`-first, Zond-if-missing authentication. It does not create a
sync job, follow pagination, change connection configuration or save the response
in the record store. Request metadata appears in `folio logs` with a `call_id`;
queries, headers and response bodies are not logged. Unknown endpoint paths are
redacted in logs because they may contain private identifiers.

```sh
# Portfolio Margin USD-M execution history, using Binance's default range:
uv run folio call binance /papi/v1/um/userTrades \
  --param symbol=ETHUSDT --param limit=1000 --verbose
# Public request without loading credentials:
uv run folio call binance /api/v3/time
uv run folio call binance /api/v3/exchangeInfo --public
# Classic Spot / USD-M / COIN-M:
uv run folio call binance /api/v3/account
uv run folio call binance /fapi/v3/positionRisk --param symbol=ETHUSDT
uv run folio call binance /dapi/v1/positionRisk --param symbol=BTCUSD_PERP
# Browser providers reuse the signed-in session:
uv run folio call bog /rest/rb --param serviceId=ACCOUNTS_GET_ACCOUNTS_AND_DETAILS
uv run folio call wolt https://consumer-api.wolt.com/order-xp/web/v1/pages/orders
# IBKR uses its existing local read API and exposure policy:
uv run folio call ibkr /v1/positions
uv run folio call ibkr /v1/contracts/details --method POST --data '{"symbol":"AAPL"}'
```

GET is supported by every adapter; IBKR also supports POST for its read queries
and accepts `--data-file PATH` for longer JSON bodies. An endpoint can contain a
query string, or repeat `--param KEY=VALUE`, including repeated parameter names.
Binance chooses the API origin from `/api/`, `/sapi/`, `/fapi/`, `/dapi/` or `/papi/`;
the exact endpoint you specify is used without account-mode probing or fallback.
Browser URLs must belong to that provider's supported origins; relative browser
paths use the captured API origin. IBKR paths stay inside its local adapter.
Redirects and automatic retries are not followed.

Binance signs calls by default; use `--public` for public endpoints. Its known
server-time endpoints automatically omit authentication and update the in-memory
clock. A signed call uses a clock already available from a sync/time call, or the
machine clock, so it sends exactly one HTTP request. On a timestamp rejection,
call the corresponding `/api/v3/time`, `/fapi/v1/time` or `/dapi/v1/time` and retry.
Browser adapters first load their application page to obtain session headers and
complete login if needed; this preparation can make additional requests before
the single requested API call.

The provider body is printed as text, preserving JSON decimal precision and
showing provider error messages. Known API keys, signing secrets, signatures and
browser session credentials echoed in a body are redacted. HTTP status is printed
to stderr; non-2xx status exits with code 1. `--verbose` also prints request logs.
Responses are bounded at 16 MiB for Binance/IBKR and 8 MiB for browser providers.
These are one-off diagnostics: approve an exact `folio call` command in Zond when
needed, rather than granting an arbitrary-endpoint rule. Lot matching and other
interpretation belong in separate tools consuming the returned data.

After updating an existing installation, run `uv sync --locked`, then
`systemctl --user restart folio.service` to load the new code. Service setup is
not needed when connection and credential-recipient configuration are unchanged.

## HTTP interface

The new service binds only to `127.0.0.1`; it has no remote authentication or TLS.
Invoke the CLI through zond/SSH for remote use. Requests from unrelated browser
origins and unexpected Host names are rejected. Local processes under other
accounts can reach the loopback listener, so use a trusted machine.

```text
GET  /v1/connections
POST /v1/connections/{name}/sync              JSON body: {}
POST /v1/connections/{name}/call              JSON body: {"endpoint":"...","params":[["key","value"]]}
GET  /v1/jobs/{id}
GET  /v1/connections/{name}/logs?after=0&limit=1000&job=...
GET  /v1/connections/{name}/records/{resource}?since=...&limit=100&offset=0
GET  /v1/connections/{name}/live/{resource}?market=usdm&symbol=ETHUSDT&from_id=0&limit=1000
```

The call response contains `id`, provider `status`, `content_type`, raw text
`body`, and metadata-only `logs`. A received provider error still returns this
envelope; transport/authentication failures return a Folio error with HTTP 502.

Live reads return `ok`, provider `status`, native `items`, `next_cursor`,
`page_full` and `scope`, plus metadata-only logs. Provider errors return
`ok: false` and the sanitized provider error; the CLI exits with status 1.
Unknown resource names and raw endpoint/header query arguments are rejected.

Follow `next_offset` to paginate stored records, or `next_after` for logs. The
optional `job` filter limits logs to that connection's job. Provider calls stay
within adapter-owned origins. There is no public profile, password-answer,
browser-control or CDP endpoint.

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
The optional zond test starts independent Folio and Zond daemons with a visible
Chromium instance,
declines a form, retries, submits a fixture password and OTP, and checks that the
secret is absent from daemon logs. These tests do not contact real accounts.
