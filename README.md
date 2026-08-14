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
accounts, positions, contract search and details, and a non-empty option chain.
It prints account and position counts but no account identifiers. To also verify
real option prices using live data with an automatic delayed-data fallback:

```console
uv run ibkr-tws-http-smoke --symbol XXI --check-market-data --expiration 20280121
```

Use `--market-data-type delayed-frozen` for the last available delayed quotes
when the market is closed.

## API

OpenAPI documentation is served at <http://127.0.0.1:8080/docs>. The endpoints
are intentionally explicit:

- `GET /healthz`
- `GET /v1/status`
- `GET /v1/accounts`
- `GET /v1/account-summary`
- `GET /v1/positions`
- `GET /v1/contracts/search?pattern=AAPL`
- `POST /v1/contracts/details`
- `GET /v1/options/chains`
- `GET /v1/options/chain-snapshot`
- `POST /v1/market-data/snapshot`
- `POST /v1/historical-bars`

Examples:

```console
curl http://127.0.0.1:8080/v1/positions

curl 'http://127.0.0.1:8080/v1/contracts/search?pattern=apple'

curl 'http://127.0.0.1:8080/v1/options/chains?underlying_symbol=AAPL&underlying_con_id=265598'

curl 'http://127.0.0.1:8080/v1/options/chain-snapshot?symbol=XXI&expiration=20280121&market_data_type=auto'

curl -X POST http://127.0.0.1:8080/v1/contracts/details \
  -H 'content-type: application/json' \
  -d '{"symbol":"AAPL","sec_type":"OPT","last_trade_date_or_contract_month":"20260918","strike":200,"right":"C"}'

curl -X POST http://127.0.0.1:8080/v1/market-data/snapshot \
  -H 'content-type: application/json' \
  -d '{"contract":{"symbol":"AAPL","sec_type":"STK"},"market_data_type":"auto"}'

curl -X POST http://127.0.0.1:8080/v1/historical-bars \
  -H 'content-type: application/json' \
  -d '{"contract":{"symbol":"AAPL"},"duration":"1 D","bar_size":"5 mins"}'
```

Use contract search to find the underlying contract ID, query its option chains
for available exchanges, trading classes, expirations, and strikes, then use
contract details to resolve a selected expiry/strike/right into a specific
option contract. Supplying an empty `exchange` (the default) returns chains
from all applicable exchanges.

Market snapshots accept `auto`, `live`, `frozen`, `delayed`, or
`delayed-frozen`. `auto` first requests live data and retries with IBKR's free
delayed feed when TWS reports missing entitlements or returns an empty snapshot.
The response identifies both the requested and actual type and exposes `as_of`,
last-trade time, bid/ask/last prices and sizes, volume, open interest, and bid,
ask, last, and model option computations containing IV and Greeks. Raw IBKR tick
fields remain available under `fields`; non-fatal partial-subscription messages
are returned under `tws_notices` with the same redaction.

The batch option endpoint resolves an underlying and expiry, then snapshots the
matching calls and puts with at most ten active requests. A request may contain
up to 40 contracts; use `strike_min` and `strike_max` to narrow larger chains.
Each quote carries either a snapshot or a structured per-contract TWS error, so
one unavailable quote does not discard the rest of the chain.

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
