# findmy-sound

A small, standalone tool that lists your Apple devices and triggers
**Play Sound** on a specifically selected device, via Apple's authenticated
iCloud Find Devices web service — as a CLI, or as a small authenticated
HTTP service Home Assistant can call (see SETUP.md).

This is **not** a Home Assistant fork, a browser scraper, or a public
service. It is a narrow, auditable wrapper around
[`pyicloud`](https://github.com/picklepete/pyicloud), reusing its mature,
non-browser (SRP-based) authentication implementation instead of
reimplementing Apple's protocol.

## What it does

- `findmy devices` — lists devices tied to your Apple Account (name, model,
  device ID, status, battery).
- `findmy play-sound --device-id DEVICE_ID` — triggers Play Sound on
  *exactly* the device with that ID, after an interactive confirmation
  (unless `--yes` is passed).
- `findmy serve` — starts the authenticated HTTP service (`POST /ring`,
  `GET /healthz`) that Home Assistant calls directly. Bearer token +
  per-profile device allowlist + rate limiting; see SETUP.md.

## What it deliberately does not do

- No browser automation.
- No Lost Mode, remote erase, remote lock, or display-message support.
- No network listener unless you start one — `findmy serve` runs the HTTP
  service (bearer token + per-profile device allowlist + rate limiting,
  see SETUP.md). Nothing listens by default.
- No name-based device targeting for Play Sound — only immutable device
  IDs are accepted, and ambiguous name lookups (`find_by_name`) always
  refuse rather than guess.

## Install

```bash
pip install -e .
```

## Usage

```bash
findmy --apple-id you@example.com devices
findmy --apple-id you@example.com play-sound --device-id abc123...
```

On first run you'll be prompted for your Apple ID password — or pass it
via `--password-source env` (`FINDMY_PASSWORD`) or a 0600
`--password-file`. If two-factor authentication is required, you'll be
prompted for the code sent to your trusted device. After a successful
2FA challenge, the session is trusted so you won't be prompted every time.

## Security notes

- **No plaintext credential storage.** Passwords are never stored at rest
  by default: they come from the OS keychain, an environment variable, a
  0600 file, or an interactive prompt — never from a world-readable file,
  and never written to disk by this tool.
- **Session files** are written by `pyicloud` into a cookie directory
  created under a restrictive `umask(0o077)`, so they are not
  world-readable.
- **Passwords, cookies, tokens, and full auth responses are never logged
  or printed.** Only a redacted device ID (`abcd...wxyz`) is logged, and
  only at `INFO` level.
- **Play Sound requires an explicit `--device-id`.** Display names can
  collide or change; the tool will not fall back to guessing a device.
- **No HTTP server starts by default.** The service only listens when you
  run `findmy serve` (see [SETUP.md](SETUP.md)).

## Architecture

```
src/findmy_sound/
├── cli.py      # argument parsing, prompts, output formatting, `serve`
├── client.py   # FindMyClient: list_devices() / play_sound(device_id)
├── auth.py     # password sources, keyring/2FA, session data dir
├── server.py   # stdlib HTTP service: POST /ring, GET /healthz
├── config.py   # per-profile + server configuration files
├── models.py   # normalized Device dataclass
└── errors.py   # user-facing exception types
```

All `pyicloud`-specific calls are isolated behind `client.py` and
`auth.py`. If Apple changes its private API, or `pyicloud` needs to be
replaced, only those two files should need to change — `cli.py` only ever
sees `Device` objects and the `findmy_sound.errors` exception types.

## Dependency

Pinned to `pyicloud==2.6.5`, the current release as of this project's
setup (verified 2026-08-04). Evaluated before pinning:

- Uses SRP (Secure Remote Password) authentication, not browser scraping.
- Discovers the Find My service URL dynamically via
  `PyiCloudService.get_webservice_url("findme")` — no hardcoded endpoint.
- Has native OS keychain integration via `keyring`.
- `pip-audit` found no known vulnerabilities in `pyicloud` or its
  transitive dependencies (`srp`, `keyring`, `requests`, `fido2`,
  `pydantic`, `tzlocal`, `certifi`) at time of writing.

Re-run `pip-audit` before upgrading, and re-verify the Find My protocol
behavior against `pyicloud`'s changelog, since Apple's private API can
change without notice:
https://github.com/picklepete/pyicloud/releases

## Testing

```bash
pip install -e ".[dev]"
pytest
```

All 71 tests use mocked `pyicloud` objects (see `tests/conftest.py`) —
**no real Apple Account or network access is used in the test suite.**
Coverage includes:

- Device listing returns correctly normalized data.
- Listing devices never triggers a sound request.
- Ambiguous device-name lookups are rejected rather than guessed.
- Missing/invalid device IDs are rejected — `play_sound` never falls back
  to an arbitrary device.
- Devices without sound support raise a clear error.
- `play_sound()`'s signature only accepts an explicit `device_id` (no
  name-based or default-device overload).
- Passwords are never written to logs.
- Both password (keyring vs. interactive prompt) and 2FA (valid, invalid,
  and empty code) authentication paths.
- The HTTP service: bearer auth required, allowlist rejection, rate
  limiting (per-profile and global), `/healthz` states, and no full
  device IDs or tokens in logs or responses.
- Per-profile and server configuration parsing (profile files, env
  overrides, missing token/profile errors).
- The auth refactor: `--data-dir` / `FINDMY_DATA_DIR` session dir,
  `env`/`file` password sources, and non-interactive login failing
  cleanly on 2FA instead of prompting.

### Live smoke test (manual, not automated in CI)

1. Run `findmy --apple-id you@example.com devices` with your own account.
2. Complete 2FA manually if prompted.
3. Confirm the device list matches what you see in Apple's Find My app.
4. Only after reviewing the list, run `play-sound --device-id ...` against
   a device you have physical access to, and confirm the sound plays.

Do not automate real Apple credentials in CI. Do not test Lost Mode,
erase, or any other irreversible action — this project does not implement
them.

## Headless invocation (Siri Shortcut / physical button)

`findmy play-sound` normally asks for confirmation and may prompt for a
password or 2FA code — fine for a terminal, not fine for something
triggering it unattended. `scripts/ring-my-device.sh` is a thin wrapper
around one pre-configured device that skips all of that, and supports
multiple people/devices via a **profile name**.

```bash
mkdir -p ~/.config/findmy-sound
cp config/ring.env.example ~/.config/findmy-sound/ring-me.env
cp config/ring.env.example ~/.config/findmy-sound/ring-partner.env
chmod 600 ~/.config/findmy-sound/ring-*.env
# edit each: set APPLE_ID and DEVICE_ID for that person's device
chmod +x scripts/ring-my-device.sh
```

Before wiring either one up, make sure you've already run
`findmy --apple-id <the relevant Apple ID> devices` interactively at least
once for each Apple ID involved — that's what saves the password to your
OS keychain and gets a 2FA-trusted session, so headless runs don't need to
prompt for anything. (If you and your partner share one Apple ID via
Family Sharing, one `devices` run covers both — you'll just see both
device IDs in the same listing.)

Then invoke by profile name:

```bash
scripts/ring-my-device.sh me
scripts/ring-my-device.sh partner
```

Each profile gets its own config file, log file
(`~/.local/state/findmy-sound/ring-<profile>.log`), and lock — so ringing
one person's phone never blocks or interferes with the other's, even if
both fire at nearly the same time. Calling the script with no profile
argument at all falls back to the original single-file layout
(`~/.config/findmy-sound/ring.env`), so existing single-device setups
keep working unchanged.

It exits non-zero on any failure (missing config, missing `findmy`
binary, lock contention, or whatever `findmy` itself returned) — useful if
whatever's calling it wants to react to success/failure.

**Why one script with a profile argument, instead of two copied scripts:**
duplicating the file means every future fix (the EOFError handling, the
locking logic, anything else) has to be made twice and will eventually
drift out of sync. A single script with a config-driven profile keeps one
source of truth. If you still want visibly distinct entry points — e.g.
because a Shortcut or webhook relay you're building expects a fixed path
with no arguments — wrap it in a one-liner instead of duplicating it:

```bash
# scripts/ring-me.sh
#!/usr/bin/env bash
exec "$(dirname "$0")/ring-my-device.sh" me
```

```bash
# scripts/ring-partner.sh
#!/usr/bin/env bash
exec "$(dirname "$0")/ring-my-device.sh" partner
```

`chmod +x` each, and now `scripts/ring-me.sh` / `scripts/ring-partner.sh`
are fixed, argument-free paths that still delegate to the one real
implementation.

**Wiring options** (this part is yours to finish, as discussed):

- **Siri Shortcut → "Run Script Over SSH"**: point it at
  `scripts/ring-my-device.sh` on whatever machine has this installed.
  Simplest option if that machine's always reachable over SSH.
- **ESP32 button**: an ESP32 can't run this script directly — point it at
  the HTTP service instead (`POST /ring` with the bearer token; see
  SETUP.md). The service holds the bar the CLI-only version sketched:
  bearer token, server-side device-ID validation, and rate limiting —
  since anything reachable by the ESP32 is a step toward "anyone on the
  network can ring your phone" if left unauthenticated.

## Service (HTTP API)

`findmy serve` starts the authenticated HTTP service (stdlib only, no new
dependencies). It reads every `ring-<profile>.env` in the config dir, binds
`0.0.0.0:8765` by default, and requires `FINDMY_BEARER_TOKEN` in the
environment. Full setup (including the per-user workflow and re-auth) is in
[SETUP.md](SETUP.md).

- `POST /ring` — body `{"profile": "me"}` or `{"device_id": "..."}`,
  header `Authorization: Bearer <token>`. The device must be in the
  profile's allowlist. Max 1 ring per 10 seconds per profile, max 10 rings
  per minute overall (excess gets 429).
- `GET /healthz` — no auth. Reports service status, auth state
  (`ok` / `needs_reauth`), last ring time, and the same per profile.

A profile is a user: one Apple ID, one device allowlist, one session store.
v1 ships with a single profile; adding another user means repeating the
setup workflow for that Apple ID — no code changes.

## CI

Two entry points run the same checks (syntax, dependency audit, guardrails
against Home Assistant imports or a web framework outside server.py, and the
mocked test suite):

**Locally, from your PATH:**

```bash
chmod +x scripts/ci.sh
ln -s "$(pwd)/scripts/ci.sh" ~/.local/bin/findmy-sound-ci   # once
findmy-sound-ci                                              # from anywhere
```

`scripts/ci.sh` resolves the repo root relative to itself, so the symlink
works regardless of your current directory. It installs the project in
editable mode if it isn't already importable, then runs:

1. Syntax check (`py_compile`)
2. Dependency audit (`pip-audit`, scoped to this project's own dependency
   chain — pyicloud and its transitive deps — so unrelated packages
   elsewhere in your environment don't fail the build)
3. Guardrail checks (no `homeassistant` imports, no web server framework
   imported outside `server.py`)
4. The mocked `pytest` suite
5. A pass/fail summary; exits non-zero on any failure

**On GitHub, automatically:** `.github/workflows/ci.yml` runs the same
`scripts/ci.sh` script on push to `main`, on every pull request, and
on-demand via the Actions tab ("Run workflow"), across Python 3.10–3.12.

## Known limitations

- Device status codes (`deviceStatus`) are not officially documented by
  Apple; only the commonly observed values are mapped to labels, others
  are shown as `Unknown (<code>)`.
- Family Sharing device visibility depends on `pyicloud`'s
  `with_family` handling and is not specifically exercised by this
  CLI beyond what `pyicloud` exposes by default.
- This project only sees devices Apple exposes through the authenticated
  iCloud web service. It cannot locate offline devices or AirTags via
  Apple's Bluetooth Find My network.
