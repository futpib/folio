# IB Gateway without VNC

This optional integration belongs to `ibkr-tws-http` and uses zondd's generic
[application-input protocol](https://github.com/futpib/zondd/blob/master/adapters/readme.md).
It runs separately from the HTTP server; the server never receives credentials.
The adapter starts IB Gateway on a private Xvfb display. A Java agent
observes Gateway's Swing login controls, selects IB API and the configured
paper/live mode, and asks zondd for username/password. Android shows the
configured recipient separately from the application's prompt. Credentials are
supplied to the original login controls only after checking that they still
belong to the same window. No coordinates, screenshots, or VNC are needed.

IBKR Mobile approval still happens in IBKR Mobile. The adapter waits up to five
minutes for the authenticated TWS API handshake. It reports ready only after the
login form is gone, API settings have been saved and verified, and the configured
API socket responds to that handshake.
A port merely accepting TCP is insufficient. Socket outages alone do not trigger
a password prompt. Reappearing login controls do.

## Install

The repository's [user-service setup](../../README.md#persistent-user-service)
installs the HTTP bridge, builds this adapter, and registers it with zond:

```sh
./scripts/setup-user-service.py --gateway paper
```

Use `--gateway live` for a live login, `--settings-dir` for existing Gateway
settings, and `--tws-port` for a nondefault API port. Setup preserves existing
configuration and validates changes before applying them. The manual steps below
are available for custom deployments.

Requirements: the Arch `ib-tws` layout (default `/usr/share/ib-tws`), a JDK 17+
for compilation, Python 3, `xvfb-run`, and `xauth`. The installed Gateway provides
its JRE and Jackson libraries. Compilation needs no downloads:

```sh
cd ~/code/ibkr-tws-http
adapters/ibgateway/build.sh
```

Merge this into `~/.config/zondd/config.toml`, adjusting absolute paths and the
Gateway API port. Existing Gateway settings can be used; run only one instance
against a settings directory. The adapter launches Gateway itself, so stop any
other launcher for that same instance before enabling it.

```toml
[credential_adapters.ibkr]
label = "IBKR Gateway — paper account"
program = "/home/me/code/ibkr-tws-http/adapters/ibgateway/launch.py"
args = ["--settings-dir", "/home/me/.ib-gateway-paper", "--mode", "paper", "--api-port", "4002"]
cwd = "/home/me"
```

For live trading choose `--mode live`, the live instance's settings and API
port, and a matching configured label. After each login, the adapter opens
Configure → Settings → API → Settings, sets the socket port, enables
**Read-Only API**, and enables localhost-only connections when that control is
available. It saves, reopens, and verifies those settings before reporting ready.
These managed values also replace conflicting values in existing settings;
unrelated settings are preserved. Gateway handles its own account-specific
settings files, including encrypted files. No manual settings step is needed.
Configure the HTTP bridge to use this same loopback port. The launcher does not
read `/etc/ib-gw.conf` or the user's
shell wrapper settings; use its explicit options. It invokes the same
`ibgateway.GWClient` entrypoint supplied by `ib-tws`.

```sh
zondd check
zondctl reload
```

Open the Android Approvals screen, select Respond on the Gateway form, enter
credentials, and approve the IBKR Mobile request when it arrives. Decline or
expiry leaves the login waiting without repeatedly prompting. Use the Retry
button or `zondctl retry-adapter ibkr` to request another form. Failed attempts
also require Retry, avoiding a credential retry loop. No password is saved by
the adapter. After reauthentication is required, a new form is generated.

The adapter recognizes the English Gateway login UI (the launcher selects
English), including the current `IBKR Gateway` window. Unknown UI layouts are
left untouched. Missing or ambiguous API settings controls prevent readiness.
It does not accept account takeover dialogs, enable trading,
or automatically dismiss arbitrary dialogs. It reports
an unavailable/failed state when it cannot finish. Additional authentication
methods can be implemented as further forms over the same generic protocol.

Keep `ibkr-tws-http` running as a separate service. It receives no credentials;
its existing reconnect loop reconnects after Gateway is ready. During login,
reads can return TWS-unavailable errors and callers can retry when ready. There
is one authentication flow for the Gateway session, independent of HTTP clients.

Gateway's own Auto Restart settings control token-preserving daily restarts.
This adapter does not configure those settings or guarantee token persistence
across a process/service restart. Weekly and server-triggered reauthentication
still require the user. Restarting zondd stops its managed adapter/Gateway group;
the bundled systemd service cleans up descendants. Run the broker as a user
service for lifecycle cleanup after a daemon crash.

## Verification

```sh
cargo build --manifest-path "$HOME/code/zondd/Cargo.toml" --workspace --bins
python3 adapters/ibgateway/test/run.py --zondd "$HOME/code/zondd/target/debug/zondd"
python3 adapters/ibgateway/test/run.py --zondd "$HOME/code/zondd/target/debug/zondd" --real
```

The first test runs an actual Swing UI under Xvfb with the production Java
agent and daemon. It verifies private delivery into controls, automatic API port
and read-only configuration, saved-setting verification, API readiness,
reauthentication, rejection/retry, withdrawal when the window disappears, and
secret-free audit storage. Its API endpoint is a local fixture and never calls
IBKR. The second starts the installed Gateway with temporary settings, observes
its real login form, and verifies rejection/retry without submitting credentials.
Neither test authenticates a live IBKR account or completes IBKR Mobile 2FA.

To exercise the complete Android and HTTP path against the installed Gateway:

```sh
ZONDD_ANDROID_DEVICE=emulator-5554 \
python3 adapters/ibgateway/test/android.py --zondd-repo "$HOME/code/zondd"
```

This requires the normal Android E2E prerequisites, unprivileged Linux user and
network namespaces, `ip`, and the HTTP repository's installed `.venv`. `FLUTTER`
and `PYTHON` can select the Flutter executable and Python with Paramiko for the
SSH authentication fixture.

The test starts the unmodified installed Gateway with a **test-only second Java
agent** that replaces its login action. Android fills the original Swing controls
through the production adapter over SSH and Iroh. Dummy credentials are checked
inside that action, and a test signal simulates accepting mobile authentication.
Post-login API settings use a Swing fixture, since the test does not authenticate
with IBKR and therefore cannot open Gateway's authenticated settings window.
Both Gateway and the real HTTP server run in a private network namespace with
only loopback, so they cannot contact IBKR. A small local TWS protocol peer supplies
the handshake and account callbacks to the HTTP server's real official API client.
The test verifies rejection/retry, unavailability before authentication, account
reads by a restricted zondd caller, session loss, another Android login, automatic
HTTP reconnection, and secret-free audit/HTTP logs. It does not test IBKR's remote
authentication service or real mobile 2FA. The patch is built into its own test
JAR; the production agent never includes it or modifies installed Gateway files.
