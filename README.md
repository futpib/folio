# IBKR TWS HTTP

`ibkr-tws-http` exposes a deliberately small, read-only HTTP API backed by a
local Interactive Brokers TWS or IB Gateway connection. It uses IBKR's official
Python `ibapi` client directly and does not include any order, cancellation, or
account-update endpoints.

This service has no authentication or TLS. It binds to `127.0.0.1:8080` by
default. If you bind it to another interface, every host that can reach it can
read every resource permitted by the exposure policy.

## Install

Python 3.11 or newer is required. One command installs the service and every
dependency:

```console
uv sync --extra dev
```

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
reports how many transient request-driven observations are currently held and
asserts `persistent_storage: false` and `all_state_transient: true`. An initial
`unknown` means the process has not yet exercised that path; it is not a claim
that an entitlement exists.

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
