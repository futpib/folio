# Folio

Folio provides local, read-only access to IBKR, Binance Spot and Futures, Bank of
Georgia personal banking, and Wolt customer order history. It adds named
connections, durable sync jobs, a SQLite data store, local credentials with Zond fallback,
and private Chromium sessions for browser providers. `folio.service` runs all
provider workers independently. It contacts Zond only for credentials missing
from `.env.local`; complete local credentials work with Zond stopped or absent.

Start with the [Folio setup and usage guide](docs/folio.md). BoG and Wolt browser
connectors are experimental and still need verification with authenticated real
accounts. Existing IBKR interfaces and exposure policies remain supported.

Run the setup wizard on the machine where Folio should run:

```sh
uv run folio setup
```

It configures any supported provider, preserves existing connections, registers
credential relays when Zond is configured, and starts the standalone user service.
For Binance with defaults:

```sh
uv run folio setup binance --yes
uv run folio sync binance --verbose
```

Use `folio config show`, `folio config set KEY VALUE`, and `folio credentials PROVIDER`
to inspect settings, change individual values, or enter credentials privately.
See the [configuration CLI guide](docs/folio.md#configuration-cli) for examples.

Binance setup creates an Ed25519 signing key for a fresh installation and prints
the public key to paste into Binance's **Self-generated** API key form. It saves
the private key in the ignored `.secrets/` directory and registers it in
`.env.local`. Reruns reuse existing keys, including imported RSA/Ed25519 keys;
existing HMAC credentials remain supported. Missing API credentials are requested
through Zond when you sync. `folio binance-keygen` and `scripts/binance-keygen.py`
remain available for standalone key generation.

`folio sync NAME --verbose` shows individual provider requests as the job runs.
Use `folio logs NAME --follow` to watch retained logs, or
`folio job ID --follow --verbose` to resume a job's progress. Logs include fixed
endpoints, timing, HTTP status and safe error codes; credentials, query strings,
headers and response bodies are excluded.

## Existing IBKR interface

`ibkr-tws-http` exposes a deliberately small, read-only HTTP API backed by a
local Interactive Brokers TWS or IB Gateway connection. It uses IBKR's official
Python `ibapi` client directly and does not include any order, cancellation, or
account-update endpoints.

This service has no authentication or TLS. It binds to `127.0.0.1:8080` by
default. If you bind it to another interface, every host that can reach it can
read every resource permitted by the exposure policy.

The companion `ibkr-tws` CLI queries that local service and prints JSON. It can
run on the same VM as TWS and be invoked through zond over SSH or Iroh, without
exposing the HTTP listener to the network.

## Install

For a persistent Linux installation, use the [user-service setup](#persistent-user-service)
below; it also installs Python dependencies, so no separate `uv sync` is needed.

Python 3.11 or newer is required. One command installs the service and every
dependency:

```console
uv sync --locked
```

This also installs `ibkr-tws` in `.venv/bin`. Use `uv run ibkr-tws` from the
checkout, or add the checkout's `.venv/bin` to your PATH. The CLI calls the
running HTTP service; it does not create another TWS API session.

The current official client is not available from IBKR as a package index or
Git dependency. The lockfile therefore pins the exact commit of an automated
[Git mirror](https://github.com/deepentropy/ibapi/tree/e27eff9c8cd9da7d91ffabaa4db86fec683956e7)
of IBKR's 10.49.2 release. Its API source was compared with the
[official download](https://interactivebrokers.github.io/downloads/twsapi_macunix.1049.02.zip):
the only difference is version-string formatting (`10.49.02` instead of
`10.49.2`). The old PyPI package named `ibapi` is not used.

In TWS, enable socket clients under **Global Configuration > API > Settings**.
Also enable TWS's **Read-Only API** setting as defense in depth. The default TWS
port here is the live-trading TWS port, `7496`.

## Run

No configuration file is required:

```console
uv run ibkr-tws-http
```

The HTTP listener can be selected on the command line:

```console
uv run ibkr-tws-http --host 127.0.0.1 --port 9080
```

The upstream TWS socket defaults to the live-trading port `7496` and can be
overridden independently:

```console
uv run ibkr-tws-http --tws-port 7497
```

To apply an exposure policy, start from [`example-config.toml`](example-config.toml):

```console
uv run ibkr-tws-http --config example-config.toml
uv run ibkr-tws-http --config example-config.toml --host 127.0.0.2 --port 9080
uv run ibkr-tws-http --config example-config.toml --check
```

`--host`, `--port`, and `--tws-port` override their TOML values. The TWS socket
itself is always restricted to a loopback host.

## Persistent user service

On a Linux host with systemd, run setup **as your login user**, from the cloned
repository. On Arch/Manjaro, install the prerequisites once:

```sh
sudo pacman -S --needed python uv
```

For an already running Gateway or TWS, one command installs and starts the bridge:

```sh
./scripts/setup-user-service.py --tws-port 4002
```

Match the port configured in Gateway/TWS. The usual defaults are Gateway paper
`4002`, Gateway live `4001`, TWS paper `7497`, and TWS live `7496`. Use `--http-port`
if the default HTTP port `8080` is occupied. HTTP remains bound to loopback.

Setup installs locked dependencies in this checkout's `.venv`, creates
`~/.config/ibkr-tws-http/config.toml`, validates configuration, and enables
`ibkr-tws-http.service`. It enables user lingering for startup at boot and
continued operation after logout. If the system requires administrator permission
for lingering, setup prints the `sudo loginctl enable-linger` command to run before
retrying. `--no-linger` explicitly skips this step for a login-session service.

The service invokes `.venv/bin/ibkr-tws-http` directly, restarts after a crash,
and reconnects automatically when Gateway becomes available. Keep the checkout
in place. Existing configuration, including account/feature restrictions, is
preserved on reruns; conflicting explicit port options fail with an explanation.
Edit the generated configuration to restrict `allowed_accounts` (empty permits
all accounts available to this login). `XDG_CONFIG_HOME` is respected when set;
it must match your systemd user manager's configuration directory.

### Include headless Gateway sign-in through zond

Install Gateway, the Java build tools, Xvfb, and a current zond release supporting
credential adapters. On Arch/Manjaro:

```sh
sudo pacman -S --needed jdk17-openjdk xorg-server-xvfb xorg-xauth
paru -S --needed ib-tws zondd-git
./scripts/setup-user-service.py --gateway paper
```

Use `--gateway live` for a live login. On a fresh installation these select API
port `4002` or `4001`; an existing HTTP configuration retains its port. Override
with `--tws-port` when necessary. To reuse previously configured Gateway settings:

```sh
./scripts/setup-user-service.py --gateway paper --settings-dir ~/.ib-gateway-paper
```

Setup builds the bundled Java agent and adds `credential_adapters.ibkr` to
`~/.config/zondd/config.toml`, preserving existing text and keeping a private
backup. It validates the complete file before applying it, starts/enables
`zondd.service`, or reloads an already running daemon, and confirms the adapter
appears in its queue. A conflicting existing `ibkr` adapter is left unchanged
for manual review. A failed application restores the previous configuration file.

The default settings directory is `~/.ib-gateway-paper` or `~/.ib-gateway-live`.
Only one launcher may own that directory; stop another Gateway launcher before
using this option. After login, the adapter configures Gateway's API socket port
to match the HTTP bridge, enables **Read-Only API**, and restricts connections to
localhost when Gateway exposes that setting. It saves and reopens the settings
to verify them before reporting ready. No manual API configuration is needed.
Credentials come from the checkout's untracked `.env.local`; missing values are
entered in the Android zond **Approvals** screen, followed by any
IBKR Mobile approval. See the [adapter guide](adapters/ibgateway/readme.md) for login/retry,
Gateway settings, and reauthentication details. The HTTP service receives no
Gateway credentials and may run while login is pending.

### Inspect, update, or remove

```sh
systemctl --user status ibkr-tws-http.service
./.venv/bin/ibkr-tws status
journalctl --user -u ibkr-tws-http.service -f
```

Setup checks HTTP health. `ibkr-tws status` must additionally show
`"tws_connected": true` before account reads work. For a custom HTTP port, pass
`--port PORT` to the CLI. Gateway logs are in `journalctl --user -u zondd.service`.

After pulling an update, rerun the same setup command to refresh dependencies
and restart the HTTP service while retaining its configuration. For Gateway
adapter changes, restart `zondd.service`: Gateway startup automatically rebuilds
the Java agent from the checkout before loading it. Keep the JDK installed;
compilation is offline, and a failed build stops startup. Restart zond when ready
to interrupt its managed jobs and Gateway session:

```sh
systemctl --user restart zondd.service
```

Use `systemctl --user edit ibkr-tws-http.service` for unit overrides. Setup owns
the generated unit; it refuses to replace a manually installed unit. Back up
and remove an earlier hand-written unit before using setup.

To remove the HTTP service while keeping the checkout and configuration:

```sh
systemctl --user disable --now ibkr-tws-http.service
rm "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/ibkr-tws-http.service"
systemctl --user daemon-reload
```

To stop managing Gateway too, remove `[credential_adapters.ibkr]` from zond's
configuration, validate with `zondd check`, and run `zondctl reload`. Other zond
services and user lingering remain available for your other user services.

## Query from the CLI

Run the HTTP service first, then use the companion CLI on the same machine:

```sh
ibkr-tws status
ibkr-tws positions
ibkr-tws account-summary
ibkr-tws contract-search AAPL
ibkr-tws contract-details --symbol AAPL
ibkr-tws snapshot --symbol AAPL --market-data-type auto
ibkr-tws option-chains AAPL 265598
ibkr-tws chain-snapshot XXI 20280121 --strike-min 2.5 --strike-max 10
ibkr-tws portfolio-snapshot --timeout-seconds 30
ibkr-tws historical-bars --symbol AAPL --duration '1 D' --bar-size '5 mins'
ibkr-tws volatility-history --symbol AAPL
ibkr-tws observed-history --con-id 829400001 --sec-type OPT
ibkr-tws volatility-comparison \
  --option-contract '{"con_id":829400001,"sec_type":"OPT"}' \
  --underlying-contract '{"symbol":"XXI"}'
```

`ibkr-tws --help` lists commands for all 16 read-only endpoints, including
`health`, `accounts`, and `capabilities`. Each command has `--help` for its
query options. Contract commands accept `--symbol`, `--con-id`, or
`--local-symbol`, plus fields such as `--sec-type OPT`, `--strike`, `--right C`,
and `--last-trade-date-or-contract-month YYYYMMDD`.

`--host` (loopback only), `--port`, and `--timeout` work before or after the
command. They select the HTTP service, not the upstream TWS socket. The default
HTTP socket timeout is 180 seconds (maximum 86400); `--timeout-seconds` separately sets a batch
query's server collection window. Proxy environment variables are ignored and
HTTP redirects are not followed.

Successful requests emit the original JSON response on stdout, preserving
numeric precision and all data-quality, delay, and partial-result fields.
HTTP success can still contain incomplete quotes or per-contract errors; inspect
those fields. Exit status is `0` for HTTP success, `1` for HTTP/connection/response
errors, `2` for invalid arguments, and `130` for interruption. Errors go to stderr
with an empty stdout, including TWS error codes when available. `status` reports
`tws_connected` in JSON; use that field to check TWS readiness. The CLI has no
prompts, stdin input, runtime files, or persistence. Responses are limited to
16 MiB.

## Run through zond on a VM

The data path is:

```text
caller -> zondctl -> SSH (optionally over Iroh) -> zondd on VM
       -> ibkr-tws on VM -> loopback HTTP -> TWS / IB Gateway
```

Install this project on the VM, keep `ibkr-tws-http` listening on loopback,
and configure zond for the Unix account that will execute the CLI. Start the
HTTP service separately so its TWS session, caches, and observations persist
between queries. The Android zond app can manage approvals and policy; these
queries are issued by `zondctl`.

After adding the installation's `.venv/bin` to PATH, run this **on the VM**:

```sh
command -v ibkr-tws
ibkr-tws health
ibkr-tws status
```

Use the reported absolute executable path in zond's policy. Zond's execution
policy matches absolute programs; the installed CLI launcher directly selects
its Python environment and works without shell activation. For example, if
the path is `/opt/folio/.venv/bin/ibkr-tws`, these entries provide fixed
read operations:

```toml
[principals.ibkr]
cwd = "/"
allow_exec = false
operations = ["ibkr-read"]

[operations.ibkr-read]
description = "Read the VM's local IBKR API"
program = "/opt/folio/.venv/bin/ibkr-tws"
fixed_args = ["--host", "127.0.0.1", "--port", "8080"]
arguments = [{ name = "query", choices = ["health", "status", "capabilities", "accounts", "account-summary", "positions", "portfolio-snapshot"] }]
decision = "allow"
```

Merge these entries into `~/.config/zondd/config.toml`, preserving other
principals and operations; validate with `zondd check` and apply to a running
daemon with `zondctl reload`. Enroll the caller's public key on the VM using
`zondctl authorize-key /path/to/caller.pub --principal ibkr` and review its
confirmation. From the caller:

```sh
zondctl --host user@vm --identity ~/.ssh/ibkr invoke ibkr-read positions
zondctl --host user@vm --identity ~/.ssh/ibkr invoke ibkr-read portfolio-snapshot
zondctl --iroh --host user@ENDPOINT_ID --identity ~/.ssh/ibkr invoke ibkr-read status
```

For arbitrary contract queries, set this principal's `allow_exec = true` and
`default = "deny"`, and add a rule requiring approval for the CLI's exact path:

```toml
[[principals.ibkr.rules]]
program = "/opt/folio/.venv/bin/ibkr-tws"
decision = "ask"
```

Then a caller can submit, for example:

```sh
zondctl --host user@vm --identity ~/.ssh/ibkr exec \
  /opt/folio/.venv/bin/ibkr-tws chain-snapshot XXI 20280121
```

Review that request in the zond app or with `zondctl review` on the VM. Fixed
named reads remain allowed. The service's exposure policy still controls
accounts, symbols, and features. Zond records command arguments and retains
bounded job output, so queries and returned account data are also subject to
the daemon's history and local access controls.

If a query exceeds zond's `command_timeout_secs`, zond backgrounds it and prints
a request ID. Use `zondctl ... follow REQUEST_ID` to obtain the result; do not
resubmit the query. Set zond's output limit large enough for the intended batch
responses. Zond does not pass stdin or allocate a terminal, which is why all
CLI query inputs are arguments. No shell wrapper or forwarded HTTP port is
needed.

## Gateway sign-in through zond

This repository's optional [IB Gateway adapter](adapters/ibgateway/readme.md)
can run Gateway on a private Xvfb display and collect login credentials through
zond's Android app, without VNC. The adapter runs as a separate process from
the HTTP server and is built separately from the Python package.
It uses the generic zond application-input protocol, with a configured recipient,
private credential delivery, and an audit trail that excludes answers.

Keep this HTTP service running independently. It never receives a Gateway
password. Configure its TWS endpoint to match the adapter's Gateway instance:

```toml
[tws]
host = "127.0.0.1"
port = 4002  # Match the Gateway instance's configured API port.
client_id = 37
reconnect_delay_seconds = 5
```

When Gateway's login screen appears, the adapter reads `IBKR_USERNAME` and
`IBKR_PASSWORD` from the checkout's `.env.local` and requests any missing fields
through zond. Complete any IBKR Mobile
approval in IBKR Mobile. Once Gateway's API becomes ready, this service's
existing supervisor reconnects automatically. During login, `ibkr-tws status`
reports `tws_connected: false` and reads can return HTTP 503; retry those reads
after reconnection. A socket outage alone does not request a password, and
HTTP callers do not create individual login flows.

Only one launcher should own the Gateway instance. When using the zond adapter,
it launches Gateway itself; do not also run another user service against the
same settings directory. Gateway's own Auto Restart settings govern reuse of
its authentication session. Restarting a launcher is not a promise of preserved
authentication.

## Live smoke test

With the service running and connected to a real TWS or IB Gateway session, run:

```console
uv run ibkr-tws-http-smoke
```

Use a different HTTP port or optionable underlying when needed:

```console
uv run ibkr-tws-http-smoke --base-url http://127.0.0.1:9080 --symbol SPY
```

The smoke test verifies the HTTP health response, TWS connection, exposed
OpenAPI contract, accounts and account summary, positions, contract search and
details, option-chain metadata, an underlying quote, and a small batch of option
quotes. It prints counts but no account identifiers. No flags are needed; live
quotes automatically fall back through delayed and delayed-frozen data.

Choose a different underlying or expiry when needed:

```console
uv run ibkr-tws-http-smoke --symbol XXI --expiration 20280121
```

Use `--skip-market-data` for a metadata-only diagnostic. Historical bars are
validated in OpenAPI but are not requested because IBKR requires live market-data
entitlements and provides no delayed historical-data fallback.

## Deterministic TWS mock

Run a local test service without TWS, subscriptions, or market hours:

```console
uv run ibkr-tws-http-mock --port 18080
```

The mock operates at the official `EClient`/`EWrapper` callback boundary, so it
exercises the production gateway rather than returning canned HTTP payloads. It
reproduces the observed no-subscription behaviors: live entitlement errors
10091/10167, delayed model-only option computations, absent bid/ask timestamps,
transient streams with no ticks, a missing underlying stream, and exact-option
historical-data errors 162/2188. Put streams return data on their second attempt,
which makes batch fairness and retry behavior deterministic.

## Transient-state invariant

All runtime state in this repository must remain transient. Implementations must
not add databases, journals, snapshot files, or any other filesystem persistence.
Restarting the process intentionally clears every cache and observation. This is
a repository-level design constraint, not merely the current default.

## In-memory cache and observations

The service keeps bounded process-local LRU caches and never writes cache entries
to disk. Contract details, contract searches, option-chain definitions, and
successful option delayed-route decisions use a five-minute TTL with at most 512
entries per cache. Successful market snapshots use a one-second, 512-entry
microcache; `collected_at` remains the collection time of the cached value.

Metadata loads are single-flight, so concurrent identical requests share one
TWS operation. Quote collection remains independently cancellable, which is
required by the batch deadline and retry behavior. Errors, accounts, account
summary, positions, and historical bars are not cached. Every cache is cleared
when the process stops or the observed TWS connection identity changes.

The service also retains at most 10,000 quality-gated option-IV observations in
process memory. Samples are added only when an API request obtains an option IV;
there is no polling loop or background recorder. The history response explicitly
labels coverage as sparse and process-local. All samples are discarded on stop
or restart.

## API

OpenAPI documentation is served at <http://127.0.0.1:8080/docs>. The endpoints
are intentionally explicit:

- `GET /healthz`
- `GET /v1/status`
- `GET /v1/capabilities`
- `GET /v1/accounts`
- `GET /v1/account-summary`
- `GET /v1/positions`
- `GET /v1/contracts/search?pattern=AAPL`
- `POST /v1/contracts/details`
- `GET /v1/options/chains`
- `GET /v1/options/chain-snapshot`
- `GET /v1/options/portfolio-snapshot`
- `POST /v1/options/volatility-comparison`
- `POST /v1/options/observed-history`
- `POST /v1/market-data/snapshot`
- `POST /v1/historical-bars`
- `POST /v1/volatility/history`

Examples:

```console
curl http://127.0.0.1:8080/v1/positions

curl 'http://127.0.0.1:8080/v1/contracts/search?pattern=apple'

curl 'http://127.0.0.1:8080/v1/options/chains?underlying_symbol=AAPL&underlying_con_id=265598'

curl 'http://127.0.0.1:8080/v1/options/chain-snapshot?symbol=XXI&expiration=20280121&market_data_type=auto&timeout_seconds=30'

curl 'http://127.0.0.1:8080/v1/options/portfolio-snapshot?market_data_type=auto&timeout_seconds=30'

curl -X POST http://127.0.0.1:8080/v1/contracts/details \
  -H 'content-type: application/json' \
  -d '{"symbol":"AAPL","sec_type":"OPT","last_trade_date_or_contract_month":"20260918","strike":200,"right":"C"}'

curl -X POST http://127.0.0.1:8080/v1/market-data/snapshot \
  -H 'content-type: application/json' \
  -d '{"contract":{"symbol":"AAPL","sec_type":"STK"},"market_data_type":"auto"}'

curl -X POST http://127.0.0.1:8080/v1/historical-bars \
  -H 'content-type: application/json' \
  -d '{"contract":{"symbol":"AAPL"},"duration":"1 D","bar_size":"5 mins"}'

curl -X POST http://127.0.0.1:8080/v1/volatility/history \
  -H 'content-type: application/json' \
  -d '{"contract":{"symbol":"AAPL"},"duration":"1 M","bar_size":"1 day"}'

curl -X POST http://127.0.0.1:8080/v1/options/volatility-comparison \
  -H 'content-type: application/json' \
  -d '{"option_contract":{"con_id":829400001,"sec_type":"OPT"},"underlying_contract":{"con_id":837468789,"symbol":"XXI","sec_type":"STK"}}'

curl -X POST http://127.0.0.1:8080/v1/options/observed-history \
  -H 'content-type: application/json' \
  -d '{"contract":{"con_id":829400001,"sec_type":"OPT"}}'
```

The volatility-history wrapper makes IBKR's two useful underlying volatility
series explicit instead of requiring callers to discover historical-data type
strings. It returns `OPTION_IMPLIED_VOLATILITY` as
`ib_30_day_at_market_implied_volatility` and `HISTORICAL_VOLATILITY` as
`ib_30_day_historical_volatility`, with values expressed as decimals. The IV is
IBKR's at-market estimate for a maturity 30 calendar days forward using two
consecutive expirations; it is not the historical IV of a particular option.
IBKR exposes these historical series for stock, ETF, and index underlyings. The
endpoint makes two bounded, one-shot historical requests and does not run a
background recorder.

The comparison endpoint pairs one current, quality-gated exact-option IV with
those underlying series. It reports premiums, ratios, a percentile, and a
z-score. Its response states that this is a comparison with IBKR's 30-day
underlying series, not reconstructed history for the exact option.

The portfolio endpoint reads the currently exposed positions, deduplicates held
option contracts, and requests only those `conId`s. It returns account aliases,
position sizes, per-contract attempts and errors, and an explicit coverage
percentage. It avoids full-chain discovery when the caller only needs held
contracts.

Use contract search to find the underlying contract ID, query its option chains
for available exchanges, trading classes, expirations, and strikes, then use
contract details to resolve a selected expiry/strike/right into a specific
option contract. Supplying an empty `exchange` (the default) returns chains
from all applicable exchanges. The batch endpoint always performs this broad
discovery first, selects the requested returned chain (normally `SMART`), and
uses that exchange only when resolving and quoting its contracts.

Market snapshots accept `auto`, `live`, `frozen`, `delayed`, or
`delayed-frozen`. `auto` first requests live data and retries with IBKR's free
delayed feed when TWS reports missing entitlements or returns no usable price,
then tries delayed-frozen data for a closed market.
The response identifies both the requested and actual type. `collected_at` is
the server collection time, while `last_trade_time` is the exchange tick time.
`market_quote_complete` means an actual bid, ask, or last price was received;
`quote_complete` remains as a compatibility alias with the same meaning.
`model_complete` means an option model price, IV, and all Greeks were received,
while `enrichment_complete` covers requested option volume, open interest, and
mark fields. `complete` requires every applicable level. A model-only option is
therefore useful but is not labelled as a market quote.

The response exposes bid/ask/last/mark prices, sizes, volume, open interest, and
bid, ask, last, and model option computations containing IV and Greeks. When
IBKR supplies an option price only through the bid, ask, or last computation,
the corresponding top-level price is populated as well. Raw IBKR tick fields
remain available under `fields`; non-fatal partial-subscription and
partial-collection messages are returned under `tws_notices` with the same
redaction. A wholly empty snapshot is an HTTP 504, not a successful response
containing null fields.

Option responses also expose a quality-gated `implied_volatility` summary. Its
`source` is `market_mid` only when an actual two-sided option quote and both
side IV computations were observed within five seconds, the price quote is not
crossed, and the bid/ask IV bounds are ordered. The response retains the bid IV,
ask IV, oldest server observation time, age at collection, and quoted-price
spread percentage. `market_mid` is the arithmetic midpoint of the bid and ask
IV bounds. If any gate fails, the value falls back to IBKR's model IV with
`source: model` and a machine-readable rejection reason; model IV is never
labelled as market IV. Delayed quotes remain identified by
`market_data_type: delayed`.

Option quotes use short-lived streaming subscriptions because IBKR rejects
generic option ticks on one-shot snapshots. The adapter collects the initial
price, volume, open-interest, IV, and Greek burst, marks deadline-limited partial
results incomplete, and always cancels the subscription. Collection ends as
soon as all applicable fields arrive, or after a fixed 500 ms capture window
from the first usable tick; continuing updates do not extend the window. If live
enrichment is unavailable, `auto` cancels that request and starts a clean
delayed stream without generic ticks. Stock and option requests use this same
short-lived streaming lifecycle so delayed quotes are not dependent on a
one-shot snapshot end callback. Delayed bid/ask/last and side option
computations can therefore produce `market_quote_complete: true`; model ticks
are reported separately and optional enrichment can remain false.

The batch option endpoint resolves an underlying and expiry, then probes one
central option before fan-out when `market_data_type=auto`. A delayed result is
reused so the other options skip redundant live-entitlement failures. Options
are scheduled before the less reliable underlying request. At most
`max_concurrency` requests (default 10) are active at once.

`timeout_seconds` selects a bounded 1–120 second global collection window; its
default is the smaller of 10 seconds and the configured TWS request timeout.
The preflight can use up to 20% of that window capped at two seconds. The
adaptive first wave divides 65% of the remaining window across enough bounded
groups to start every contract before any retry. Only timed-out or unresolved
contracts enter the final wave, again with a fair per-contract slice. Slow early
contracts therefore cannot consume every worker twice while later strikes never
start.
`collection_timeout_seconds`, `preflight_market_data_type`,
`retried_contracts`, and each row's `attempts` make that behavior observable.

`collection_started_at` and `collected_at` bound the collection cohort, and the
embedded underlying snapshot has its own exchange tick time. If the direct
underlying remains unavailable, `reference_underlying_price` is the median of
the returned option model underlying prices and is explicitly labelled with
`reference_underlying_price_source: option_model_median`, sample count, and
collection time. It is a fallback reference, not a direct stock quote. A request
may contain up to 40 option contracts; use `strike_min` and `strike_max` to
narrow larger chains. Each quote carries either a snapshot or a structured
per-contract TWS error, so one unavailable strike does not discard the rest of
the chain. Batch market-quote, model, enrichment, and aggregate completeness are
reported separately. Missing live subscriptions remain visible through TWS
error codes; the service does not manufacture live data from delayed ticks.

`GET /v1/capabilities` summarizes evidence observed by this process. It reports
whether live quotes, delayed option models, and exact-option historical data are
available, including the TWS codes supporting an unavailable result. It also
reports how many transient request-driven observations are currently held. An
initial `unknown` means the process has not yet exercised that path; it is not a
claim that an entitlement exists.

The default policy redacts account IDs as `account`. Configure an alias to make
multiple accounts distinguishable without returning their real IDs. Empty
symbol and exchange allowlists mean “allow all”; the security-type allowlist is
enabled by default.

## Read-only boundary

The public app only references the read methods defined by its `Gateway`
protocol. The official adapter implements market/account requests and their
matching cancellation calls; it contains no placement, modification, exercise,
or global-cancel operation. Rejected requests return the exact TWS error code and
a length-limited message with IBKR account identifiers redacted.

Market-data permissions and pacing rules still apply. Requests time out rather
than keeping abandoned TWS subscriptions alive, and the adapter reconnects when
the local TWS session drops.
