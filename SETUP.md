# findmy-sound setup

One profile is one user: an Apple ID, a device allowlist, and a session
store. v1 ships with a single profile (Nate). Adding another user means
repeating the per-user setup below for that person's Apple ID — no code
changes, and each user's config and session stay separate.

## Prerequisites

- Python 3.10+ and this repo (`pip install -e ".[dev]"` for tests).
- One Apple ID per user, with a trusted iPhone (or other Apple device)
  that can receive 2FA codes.

## 1. Bearer token

The HTTP service requires a bearer token. Generate one and keep it in
the service env file only — never in the repo, never in a profile file:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Put it in the service env file (0600, on the host only). If the service
runs under systemd, that file is the unit's `EnvironmentFile`:

```bash
# /etc/findmy-sound/service.env  (chmod 600, owned by the service user)
FINDMY_BEARER_TOKEN=<paste the token here>
FINDMY_DATA_DIR=/var/lib/findmy-sound/sessions
```

## 2. Per-user setup (repeat for each Apple ID)

### a. Create the profile file

```bash
mkdir -p ~/.config/findmy-sound
cp config/ring.env.example ~/.config/findmy-sound/ring-me.env
chmod 600 ~/.config/findmy-sound/ring-me.env
# Edit it: set APPLE_ID and DEVICE_ID for that user's device.
```

The profile name is the filename (`ring-me.env` → profile `me`). A
profile may also list several devices with `DEVICE_IDS` (comma-separated);
`DEVICE_ID` and `DEVICE_IDS` combine into that profile's allowlist — the
service only ever rings device IDs listed here.

### b. Authenticate once, interactively

Run the CLI once for that Apple ID and complete 2FA at the terminal.
This establishes a trusted session in that profile's data dir
(`<data-dir>/<profile>/`):

```bash
findmy --apple-id you@icloud.com --data-dir ~/.local/state/findmy-sound/sessions/me devices
```

No password is stored at rest: the trusted session is what persists.
If the session later expires, repeat this step for that Apple ID — the
service keeps running and reports `needs_reauth` until you do.

### c. Verify

```bash
findmy --apple-id you@icloud.com --data-dir ~/.local/state/findmy-sound/sessions/me play-sound --device-id <id> --yes
```

## 3. Start the service

```bash
findmy serve --config-dir ~/.config/findmy-sound
```

It reads every `ring-<profile>.env` in the config dir, binds
`0.0.0.0:8765` by default, and requires `FINDMY_BEARER_TOKEN` in the
environment. Adding a profile means adding its file and restarting
the service. The service logs redacted device IDs only.

Endpoints:

- `POST /ring` — body `{"profile": "me"}` or `{"device_id": "..."}`,
  header `Authorization: Bearer <token>`. The device must be in the
  profile's allowlist. Max 1 ring per 10 seconds per profile, max 10
  rings per minute overall (excess gets 429).
- `GET /healthz` — no auth. Reports service status, auth state
  (`ok` / `needs_reauth`), last ring time, and the same per profile.

## 4. Re-authenticating (session expired)

`GET /healthz` reports `needs_reauth` and ring requests fail with a
clear 503. To fix it, SSH to the host and repeat step 2b for that
Apple ID — the 2FA code still arrives on the trusted iPhone. The
service resumes without a restart.

## 5. Adding a second user

Repeat step 2 for the new person's Apple ID with a new profile name
(e.g. `ring-partner.env`). Their config and session live separately
from the first user's; the service picks the new profile up on restart.
Then point Home Assistant at it (see below). Nothing else changes.

## 6. Home Assistant wiring

Point the ATOM-button automations at the service with `rest_command`
(instead of the old `shell_command` SSH path):

```yaml
rest_command:
  ring_me:
    url: http://<host>:8765/ring
    method: POST
    headers:
      Authorization: "Bearer <token>"
      Content-Type: "application/json"
    payload: '{"profile": "me"}'
```

Add one `rest_command` per profile. The old SSH automations stay
 disabled (not deleted) until the HTTP path has proven itself.

## Security notes

- Never commit a real profile env file, the service env file, or a
  bearer token. `config/ring.env` is gitignored; the token lives only
  in the host's service env file and in Home Assistant.
- The service binds the LAN (`0.0.0.0:8765`) because HA and the ESP32s
  are separate hosts. The bearer token is what makes that safe; treat
  it like a password and rotate it if it ever leaks.
- Logs never contain full device IDs, tokens, passwords, or cookies.
